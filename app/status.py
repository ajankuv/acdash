"""Data-freshness status (``GET /status``), separate from liveness (``GET /health``).

``/health`` answers "is the web server up" and stays 200 — the container healthcheck and CI
rely on it. ``/status`` answers "is the data fresh": 503 when no successful AC Infinity fetch
has happened for ``STATUS_STALE_SECONDS`` (default 3 × the collector interval), so an uptime
monitor pointed at it alerts when the dashboard is up but showing old data.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

_lock = threading.Lock()
_state: dict[str, Any] = {"last_success": None, "last_error": None, "last_error_at": None}
_started = time.time()


def record_success(now: float | None = None) -> None:
    with _lock:
        _state["last_success"] = time.time() if now is None else now


def record_error(message: str, now: float | None = None) -> None:
    with _lock:
        _state["last_error"] = message
        _state["last_error_at"] = time.time() if now is None else now


def stale_after_seconds(collector_interval: int) -> float:
    raw = (os.environ.get("STATUS_STALE_SECONDS") or "").strip()
    try:
        if raw:
            return max(1.0, float(raw))
    except ValueError:
        pass
    return float(3 * max(1, collector_interval))


def snapshot(collector_interval: int, *, now: float | None = None,
             session_active: bool | None = None, backfill: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """``(http_status, body)`` for ``/status``. Contains no secrets or device identifiers."""
    now = time.time() if now is None else now
    limit = stale_after_seconds(collector_interval)
    with _lock:
        last, err, err_at = _state["last_success"], _state["last_error"], _state["last_error_at"]
    since = None if last is None else round(now - last, 1)
    if since is not None and since <= limit:
        state, code = "ok", 200
    elif last is None and now - _started <= limit:
        state, code = "starting", 200
    else:
        state, code = "stale", 503
    body = {
        "state": state,
        "last_success": last,
        "seconds_since_success": since,
        "stale_after_seconds": limit,
        "last_error": err if (err_at is not None and (last is None or err_at >= last)) else None,
        "session_active": session_active,
        "uptime_seconds": round(now - _started, 1),
    }
    if backfill is not None:
        body["backfill"] = {k: backfill.get(k) for k in
                            ("enabled", "running", "last_run_finished", "last_rows_inserted", "last_error")}
    return code, body


def _reset(started: float | None = None) -> None:
    """Test helper."""
    global _started
    with _lock:
        _state.update(last_success=None, last_error=None, last_error_at=None)
    _started = time.time() if started is None else started
