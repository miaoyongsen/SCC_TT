"""WAF block proxy: public 8081 -> authenticated SCC 8000 WAF API.

Only forwards validated block_ip. Secrets never appear in responses or logs.
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import re
from typing import Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("scc_tt.waf")
router = APIRouter()

DEFAULT_SCC_WAF_URL = "http://127.0.0.1:8000/api/v1/waf/requests"
MAX_IP_ITEMS = 100


class WafBlockPayload(BaseModel):
    block_ip: str = Field(..., min_length=1, max_length=20000)
    token: str = Field(..., min_length=1)


def verify_token(supplied: str) -> None:
    expected = os.getenv("WAF_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="WAF proxy is not configured")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid WAF token")


def validate_block_ip(value: str, *, single: bool = False) -> str:
    # Preserve valid client-supplied IP/CIDR strings; SCC validates again.
    items = [s.strip() for s in re.split(r"[,，;；\s]+", value) if s.strip()]
    if not items:
        raise HTTPException(status_code=422, detail="block_ip is required")
    if single and len(items) != 1:
        raise HTTPException(status_code=422, detail="GET accepts exactly one IP/CIDR")
    if len(items) > MAX_IP_ITEMS:
        raise HTTPException(status_code=422, detail="too many IP/CIDR entries (max 100)")
    for item in items:
        try:
            ipaddress.ip_network(item, strict=False)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid IP/CIDR: {item}")
    return ",".join(dict.fromkeys(items))


def upstream_config() -> tuple[str, str]:
    token = os.getenv("SCC_API_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=503, detail="SCC_API_TOKEN is not configured")
    url = os.getenv("SCC_WAF_URL", DEFAULT_SCC_WAF_URL).strip()
    parsed = urlsplit(url)
    # Never send the privileged SCC API token to a URL with credentials,
    # fragments, query strings, or an unexpected API path.
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path != "/api/v1/waf/requests"):
        raise HTTPException(status_code=503, detail="SCC_WAF_URL configuration is invalid")
    return url, token


async def forward(block_ip: str) -> JSONResponse:
    url, token = upstream_config()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=5.0),
                                     follow_redirects=False,
                                     trust_env=False) as client:
            response = await client.post(
                url,
                json={"block_ip": block_ip},
                headers={"Authorization": "Bearer " + token},
            )
    except httpx.TimeoutException:
        # SCC may already have written to S3 even if this connection timed out.
        logger.warning("WAF upstream timeout; outcome may be unknown")
        raise HTTPException(
            status_code=504,
            detail="SCC response timeout; submission status unknown, do not retry blindly",
        )
    except httpx.RequestError as exc:
        logger.warning("WAF upstream network error: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="SCC connection failed")

    if 300 <= response.status_code < 400:
        raise HTTPException(status_code=502, detail="unexpected SCC redirect")
    try:
        payload = response.json()
    except ValueError:
        logger.error("WAF upstream returned non-JSON response, status=%s", response.status_code)
        raise HTTPException(status_code=502, detail="SCC returned a non-JSON response")
    # Return SCC's original JSON and HTTP status; never forward upstream headers.
    return JSONResponse(status_code=response.status_code, content=payload)


@router.get("/waf_ip/update")
async def waf_get(
    block_ip: str = Query(..., min_length=1, max_length=20000),
    token: str = Query(..., min_length=1),
):
    verify_token(token)
    return await forward(validate_block_ip(block_ip, single=True))


@router.post("/waf_ip/update")
async def waf_post(body: WafBlockPayload):
    verify_token(body.token)
    return await forward(validate_block_ip(body.block_ip))
