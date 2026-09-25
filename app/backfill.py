"""Fill holes in local history from AC Infinity's cloud history (``log/dataPage``).

The cloud keeps ~1-minute rows for at least 90 days. Local SQLite history only exists while
acdash is running, so container downtime (updates, reboots) and new installs leave gaps.
At startup and every 6 hours, find gaps > 5 min in the last ``BACKFILL_DAYS`` per controller
and fill them one day per request. Local readings are never modified.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Callable

from app import storage
from app.history import history_row_to_point

if TYPE_CHECKING:
    from app.client import ACInfinityClient

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 30
MAX_DAYS = 90
MIN_GAP_SECS = 300
CHUNK_SECS = 86_400          # the API rate-limits spans over ~24 h
RECENT_MARGIN_SECS = 120     # leave the last couple of minutes to the live collector
INTERVAL_SECS = 6 * 3600
STARTUP_DELAY_SECS = 20
PAGE_SIZE = 2000

status: dict[str, Any] = {
    "enabled": None,
    "days": None,
    "running": False,
    "last_run_started": None,
    "last_run_finished": None,
    "last_rows_inserted": 0,
    "total_rows_inserted": 0,
    "requests": 0,
    "last_error": None,
}


def backfill_days() -> int:
    raw = (os.environ.get("BACKFILL_DAYS") or "").strip()
    if not raw:
        return DEFAULT_DAYS
    try:
        return max(0, min(MAX_DAYS, int(raw)))
    except ValueError:
        logger.warning("Invalid BACKFILL_DAYS=%r; using %d", raw, DEFAULT_DAYS)
        return DEFAULT_DAYS


def _chunks(start: int, end: int) -> list[tuple[int, int]]:
    out = []
    t = start
    while t < end:
        out.append((t, min(end, t + CHUNK_SECS)))
        t += CHUNK_SECS
    return out


def backfill_controller(client: "ACInfinityClient", dev_id: str, *, now: int | None = None,
                        days: int | None = None) -> tuple[int, int]:
    """Fill one controller's gaps. Returns ``(rows_inserted, requests_made)``."""
    days = backfill_days() if days is None else days
    if days <= 0:
        return 0, 0
    now = int(time.time()) if now is None else now
    until = now - RECENT_MARGIN_SECS
    since = now - days * 86_400
    inserted = requests = 0
    for gap_start, gap_end in storage.find_gaps(dev_id, since, until, MIN_GAP_SECS):
        for c_start, c_end in _chunks(gap_start, gap_end):
            requests += 1
            data = client.history_data_page(dev_id, c_end, c_start, page_size=PAGE_SIZE, order_direction=1)
            if not data:
                logger.info("backfill: no cloud data for %s %d–%d (%s)", dev_id, c_start, c_end,
                            getattr(client, "last_request_error", None) or "empty")
                continue
            points = []
            for row in data.get("rows") or []:
                pt = history_row_to_point(row)
                # Strictly inside the gap: the readings at its edges already exist locally.
                if pt and gap_start < pt["t"] < gap_end:
                    points.append(pt)
            inserted += storage.insert_cloud_readings(dev_id, points)
    return inserted, requests


def run_once(client: "ACInfinityClient", dev_ids: list[str], *, now: int | None = None) -> int:
    """One backfill pass over all controllers (blocking). Returns rows inserted."""
    days = backfill_days()
    status.update(enabled=days > 0, days=days)
    if days <= 0 or not dev_ids:
        return 0
    status.update(running=True, last_run_started=int(time.time()), last_error=None)
    total = 0
    try:
        for dev_id in dev_ids:
            rows, reqs = backfill_controller(client, dev_id, now=now, days=days)
            total += rows
            status["requests"] += reqs
        if total:
            logger.info("backfill: inserted %d cloud reading(s) across %d controller(s)", total, len(dev_ids))
    except Exception as exc:  # noqa: BLE001 — never let backfill take the app down
        status["last_error"] = f"{type(exc).__name__}: {exc}"
        logger.exception("backfill pass failed")
    finally:
        status.update(running=False, last_run_finished=int(time.time()), last_rows_inserted=total)
        status["total_rows_inserted"] += total
    return total


async def backfill_loop(
    get_client_fn: Callable[[], "ACInfinityClient | None"],
    get_dev_ids_fn: Callable[[], list[str]],
    *,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> None:
    """Startup pass (after a short delay) then every 6 h. Disabled when BACKFILL_DAYS=0."""
    status.update(enabled=backfill_days() > 0, days=backfill_days())
    if backfill_days() <= 0:
        logger.info("backfill disabled (BACKFILL_DAYS=0)")
        return
    await sleep(STARTUP_DELAY_SECS)
    while True:
        try:
            client = await asyncio.to_thread(get_client_fn)
            dev_ids = await asyncio.to_thread(get_dev_ids_fn) if client else []
            if client and dev_ids:
                await asyncio.to_thread(run_once, client, dev_ids)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            status["last_error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("backfill loop error")
        await sleep(INTERVAL_SECS)


def debug_status() -> dict[str, Any]:
    return {**status, "oldest_local_ts": storage.oldest_reading_ts()}
