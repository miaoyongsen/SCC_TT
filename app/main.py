from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve()
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "webhook.db"))).resolve()
LOG_DIR = Path(os.getenv("LOG_DIR", str(BASE_DIR / "logs"))).resolve()
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8081"))
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(10 * 1024 * 1024)))
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Shanghai"))

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
write_lock = threading.Lock()

app = FastAPI(title="SCC_TT Webhook Receiver", version="1.0.0")


def now_local() -> datetime:
    return datetime.now(TIMEZONE)


def table_name(month: str) -> str:
    if not re.fullmatch(r"\\d{6}", month):
        raise HTTPException(status_code=400, detail="month must use YYYYMM format")
    year, mm = int(month[:4]), int(month[4:])
    if year < 2000 or not 1 <= mm <= 12:
        raise HTTPException(status_code=400, detail="invalid month")
    return f"webhook_{month}"


@contextmanager
def db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = FULL")
        yield conn
    finally:
        conn.close()


def initialize_database() -> None:
    with db_connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        # FULL is more conservative for durability before acknowledging a webhook.
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) "
            "VALUES('schema_version', '1')"
        )
        conn.commit()


def ensure_month_table(conn: sqlite3.Connection, month: str) -> str:
    table = table_name(month)
    conn.execute(
        f'''CREATE TABLE IF NOT EXISTS "{table}" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            received_at TEXT NOT NULL,
            pushed INTEGER NOT NULL DEFAULT 0 CHECK (pushed IN (0, 1))
        )'''
    )
    conn.execute(
        f'CREATE INDEX IF NOT EXISTS "idx_{table}_received_at" '
        f'ON "{table}"(received_at)'
    )
    conn.execute(
        f'CREATE INDEX IF NOT EXISTS "idx_{table}_pushed" '
        f'ON "{table}"(pushed)'
    )
    return table


@app.on_event("startup")
def startup() -> None:
    initialize_database()
    if not WEBHOOK_TOKEN:
        logger.warning(
            "WEBHOOK_TOKEN is not configured; webhook endpoint accepts unauthenticated requests"
        )
    logger.info("SCC_TT started; database=%s", DB_PATH)


@app.get("/health")
def health() -> dict[str, Any]:
    with db_connection() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "service": "SCC_TT", "time": now_local().isoformat()}


@app.post("/webhook")
async def receive_webhook(
    request: Request,
    x_webhook_token: str | None = Header(default=None),
) -> JSONResponse:
    if WEBHOOK_TOKEN and x_webhook_token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="invalid webhook token")

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"payload too large; limit is {MAX_BODY_BYTES} bytes",
        )
    if not body:
        raise HTTPException(status_code=400, detail="empty request body")

    # Preserve valid UTF-8 payloads exactly; encode binary payloads losslessly.
    try:
        content = body.decode("utf-8")
    except UnicodeDecodeError:
        content = json.dumps(
            {"_encoding": "base64", "data": base64.b64encode(body).decode("ascii")}
        )

    received_dt = now_local()
    received_at = received_dt.isoformat(timespec="milliseconds")
    month = received_dt.strftime("%Y%m")

    def persist() -> int:
        # Single-process lock + one Uvicorn worker prevents local write races.
        with write_lock:
            with db_connection() as conn:
                table = ensure_month_table(conn, month)
                cursor = conn.execute(
                    f'INSERT INTO "{table}" (content, received_at, pushed) '
                    "VALUES (?, ?, 0)",
                    (content, received_at),
                )
                conn.commit()
                return int(cursor.lastrowid)

    try:
        record_id = await run_in_threadpool(persist)
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "id": record_id,
                "month": month,
                "received_at": received_at,
            },
        )
    except sqlite3.Error:
        logger.exception("failed to persist webhook payload")
        raise HTTPException(
            status_code=503,
            detail="storage temporarily unavailable; sender should retry",
        )


@app.get("/records")
def list_records(
    month: str | None = Query(default=None, description="YYYYMM; defaults to current month"),
    pushed: int | None = Query(default=None, ge=0, le=1),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    month = month or now_local().strftime("%Y%m")
    table = table_name(month)
    where = " WHERE pushed = ?" if pushed is not None else ""
    filter_params: list[Any] = [pushed] if pushed is not None else []
    with db_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not exists:
            return {
                "month": month,
                "total": 0,
                "limit": limit,
                "offset": offset,
                "items": [],
            }
        total = conn.execute(
            f'SELECT COUNT(*) FROM "{table}"{where}', filter_params
        ).fetchone()[0]
        rows = conn.execute(
            f'SELECT id, content, received_at, pushed FROM "{table}"{where} '
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            [*filter_params, limit, offset],
        ).fetchall()
    return {
        "month": month,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [dict(row) for row in rows],
    }


@app.get("/records/{record_id}")
def get_record(
    record_id: int,
    month: str = Query(..., description="Month in YYYYMM format"),
) -> dict[str, Any]:
    table = table_name(month)
    with db_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="record not found")
        row = conn.execute(
            f'SELECT id, content, received_at, pushed FROM "{table}" WHERE id=?',
            (record_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="record not found")
    return dict(row)


@app.patch("/records/{record_id}/push-status")
def update_push_status(
    record_id: int,
    month: str = Query(..., description="Month in YYYYMM format"),
    pushed: int = Query(..., ge=0, le=1),
) -> dict[str, Any]:
    table = table_name(month)
    with write_lock:
        with db_connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail="record not found")
            cursor = conn.execute(
                f'UPDATE "{table}" SET pushed=? WHERE id=?',
                (pushed, record_id),
            )
            conn.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="record not found")
    return {"ok": True, "id": record_id, "month": month, "pushed": pushed}


@app.get("/stats")
def stats(
    month: str | None = Query(default=None, description="YYYYMM; defaults to current month"),
) -> dict[str, Any]:
    month = month or now_local().strftime("%Y%m")
    table = table_name(month)
    with db_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not exists:
            return {"month": month, "total": 0, "pending": 0, "pushed": 0}
        row = conn.execute(
            f'SELECT COUNT(*) AS total, '
            f'SUM(CASE WHEN pushed=0 THEN 1 ELSE 0 END) AS pending, '
            f'SUM(CASE WHEN pushed=1 THEN 1 ELSE 0 END) AS pushed '
            f'FROM "{table}"'
        ).fetchone()
    return {
        "month": month,
        "total": row["total"],
        "pending": row["pending"] or 0,
        "pushed": row["pushed"] or 0,
    }
