from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, List, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Request
from asyncio import QueueFull
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve()
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "webhook.db"))).resolve()
LOG_DIR = Path(os.getenv("LOG_DIR", str(BASE_DIR / "logs"))).resolve()
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8081"))
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(10 * 1024 * 1024)))
QUEUE_MAX_SIZE = int(os.getenv("QUEUE_MAX_SIZE", "5000"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "200"))
BATCH_WAIT_MS = int(os.getenv("BATCH_WAIT_MS", "10"))
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Shanghai"))

if QUEUE_MAX_SIZE < 1 or BATCH_SIZE < 1 or BATCH_WAIT_MS < 0:
    raise ValueError("QUEUE_MAX_SIZE and BATCH_SIZE must be positive; BATCH_WAIT_MS cannot be negative")

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "service.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("scc_tt")


def now_local() -> datetime:
    return datetime.now(TIMEZONE)


def table_name(month: str) -> str:
    if not re.fullmatch(r"\d{6}", month):
        raise ValueError("month must use YYYYMM format")
    year, mm = int(month[:4]), int(month[4:])
    if year < 2000 or not 1 <= mm <= 12:
        raise ValueError("invalid month")
    return f"webhook_{month}"


@contextmanager
def db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level="DEFERRED")
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = FULL")
        yield conn
    finally:
        conn.close()


def initialize_database() -> None:
    with db_connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.commit()


def ensure_month_table(conn: sqlite3.Connection, month: str) -> str:
    table = table_name(month)
    conn.execute(
        f'''CREATE TABLE IF NOT EXISTS "{table}" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            received_at TEXT NOT NULL
        )'''
    )
    conn.execute(
        f'CREATE INDEX IF NOT EXISTS "idx_{table}_received_at" '
        f'ON "{table}"(received_at)'
    )
    return table


@dataclass
class PendingItem:
    content: str
    received_at: str
    month: str


write_queue: asyncio.Queue[Optional[PendingItem]] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
writer_task: Optional[asyncio.Task] = None


def persist_batch(items: List[PendingItem]) -> None:
    # A single transaction for a batch substantially reduces per-event disk overhead.
    with db_connection() as conn:
        for item in items:
            table = ensure_month_table(conn, item.month)
            conn.execute(
                f'INSERT INTO "{table}" (content, received_at) VALUES (?, ?)',
                (item.content, item.received_at),
            )
        conn.commit()


async def database_writer() -> None:
    logger.info(
        "database writer started: queue=%s batch=%s wait_ms=%s",
        QUEUE_MAX_SIZE, BATCH_SIZE, BATCH_WAIT_MS,
    )
    stopping = False
    while not stopping:
        first = await write_queue.get()
        if first is None:
            write_queue.task_done()
            break

        batch = [first]
        deadline = asyncio.get_running_loop().time() + BATCH_WAIT_MS / 1000

        while len(batch) < BATCH_SIZE:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                item = await asyncio.wait_for(write_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if item is None:
                write_queue.task_done()
                stopping = True
                break
            batch.append(item)

        try:
            await run_in_threadpool(persist_batch, batch)
        except Exception:
            logger.exception("SQLite batch write failed for %s webhook(s)", len(batch))
            # HTTP requests are acknowledged after bounded-queue admission.
            # Keep storage failures visible in service.log for operator action.
        else:
            logger.info("SQLite batch committed: count=%s remaining_queue=%s", len(batch), write_queue.qsize())
        finally:
            for _ in batch:
                write_queue.task_done()

    logger.info("database writer stopped")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global writer_task
    initialize_database()
    if not WEBHOOK_TOKEN:
        logger.warning("WEBHOOK_TOKEN is not configured; webhook endpoint accepts unauthenticated requests")
    writer_task = asyncio.create_task(database_writer(), name="sqlite-batch-writer")
    logger.info("SCC_TT started; database=%s", DB_PATH)
    try:
        yield
    finally:
        # Drain accepted events and flush them before process shutdown.
        await write_queue.put(None)
        if writer_task is not None:
            await writer_task
            writer_task = None


app = FastAPI(title="SCC_TT Webhook Receiver", version="1.1.0", lifespan=lifespan)


@app.post("/scc_msoc/log")
async def receive_webhook(
    request: Request,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> JSONResponse:
    if WEBHOOK_TOKEN:
        scheme, separator, supplied_token = (authorization or "").partition(" ")
        if not separator or scheme.lower() != "bearer" or supplied_token.strip() != WEBHOOK_TOKEN:
            raise HTTPException(status_code=401, detail="invalid bearer token")

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"payload too large; limit is {MAX_BODY_BYTES} bytes")
    if not body:
        raise HTTPException(status_code=400, detail="empty request body")

    # Keep UTF-8 request bodies byte-for-byte as text; preserve binary payloads as base64.
    try:
        content = body.decode("utf-8")
    except UnicodeDecodeError:
        content = json.dumps(
            {"_encoding": "base64", "data": base64.b64encode(body).decode("ascii")}
        )

    received_dt = now_local()
    received_at = received_dt.isoformat(timespec="milliseconds")
    month = received_dt.strftime("%Y%m")
    item = PendingItem(content, received_at, month)

    # Acknowledge immediately after bounded-queue admission. SQLite batching runs
    # in the background so slow disk commits do not consume the sender's HTTP
    # response timeout. If the queue is full, return 503 so the sender can retry.
    try:
        write_queue.put_nowait(item)
    except QueueFull:
        logger.warning(
            "webhook queue full; rejecting request from %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=503,
            detail="receiver queue is full; retry later",
            headers={"Retry-After": "2"},
        )

    logger.info(
        "webhook accepted: received_at=%s month=%s bytes=%s queue_size=%s",
        received_at, month, len(body), write_queue.qsize(),
    )
    return JSONResponse(
        status_code=200,
        content={"ok": True, "accepted": True, "received_at": received_at},
    )
