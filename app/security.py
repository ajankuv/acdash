"""Request safety for a LAN dashboard that physically drives fans and outlets.

- Cross-site protection on state-changing methods using browser fetch metadata
  (``Sec-Fetch-Site``) and ``Origin``. No sessions/cookies exist, so token CSRF doesn't fit;
  requests without these headers (curl, scripts) aren't browser cross-site requests and pass.
- JSON routes only accept ``application/json`` bodies (a cross-site form can send
  ``text/plain`` without a CORS preflight, and ``request.json()`` would parse it anyway).
- Safe default response headers; frame blocking is opt-in.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
BLOCKED_FETCH_SITES = frozenset({"cross-site", "same-site"})


def _netloc(origin: str) -> str:
    try:
        return urlsplit(origin.strip()).netloc.lower()
    except ValueError:
        return ""


def trusted_origins() -> set[str]:
    raw = os.environ.get("ACDASH_TRUSTED_ORIGINS") or ""
    return {n for n in (_netloc(o) for o in raw.split(",") if o.strip()) if n}


def _own_hosts(request: Request) -> set[str]:
    hosts = {(request.headers.get("host") or "").lower()}
    if (os.environ.get("ACDASH_TRUST_PROXY_HEADERS") or "").strip() == "1":
        fwd = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip().lower()
        if fwd:
            hosts.add(fwd)
    hosts.discard("")
    return hosts


def cross_site_reason(request: Request) -> str | None:
    """Why this state-changing request must be refused, or None if it's allowed."""
    if request.method not in UNSAFE_METHODS:
        return None
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site in BLOCKED_FETCH_SITES:
        return f"Sec-Fetch-Site: {site}"
    origin = request.headers.get("origin")
    if origin is not None:
        net = _netloc(origin) if origin.strip().lower() != "null" else ""
        if not net or net not in (_own_hosts(request) | trusted_origins()):
            return f"Origin {origin!r} is not this dashboard (set ACDASH_TRUSTED_ORIGINS if it should be)"
    return None


class RequestSafetyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any):
        reason = cross_site_reason(request)
        if reason is not None:
            logger.warning("Refused cross-site %s %s (%s)", request.method, request.url.path, reason)
            response = JSONResponse({"error": "Cross-site request refused", "reason": reason}, status_code=403)
        else:
            response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        frame = (os.environ.get("ACDASH_FRAME_OPTIONS") or "").strip().upper()
        if frame in ("DENY", "SAMEORIGIN"):
            response.headers.setdefault("X-Frame-Options", frame)
        return response


async def read_json_body(request: Request) -> tuple[Any, JSONResponse | None]:
    """Parse a JSON body, refusing non-JSON content types (415) and bad JSON (400)."""
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return None, JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)
    try:
        body = await request.json()
    except Exception:
        return None, JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return None, JSONResponse({"error": "JSON body must be an object"}, status_code=400)
    return body, None
