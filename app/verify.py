"""Confirm a port write against the controller's LIVE state, not the stored setting.

A 200 from addDevMode — and even a settings read-back showing the new value — does not prove
the controller applied it (dalinicus HA #166: cloud stores onSpead, device ignores it). After a
write, poll ``devInfoListAll`` (the live snapshot the dashboard cards use) until the port
matches the request or the window ends.

Match rules:
  Off          → live speed 0 / loadState 0
  Manual On    → live speed == requested speed
  Other modes  → live port mode (curMode) == requested atType; speed depends on conditions
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from app.client import ACInfinityClient

logger = logging.getLogger(__name__)

POLL_SECONDS = 10.0
_MAX_RESULTS = 50

_results: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_lock = threading.Lock()


def verify_window_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("WRITE_VERIFY_SECONDS", "90")))
    except ValueError:
        return 90.0


def _store(vid: str, data: dict[str, Any]) -> None:
    with _lock:
        _results[vid] = data
        _results.move_to_end(vid)
        while len(_results) > _MAX_RESULTS:
            _results.popitem(last=False)


def get_result(vid: str) -> dict[str, Any] | None:
    with _lock:
        r = _results.get(vid)
        return dict(r) if r else None


def _live_port(devices: list[dict[str, Any]], dev_id: str, port: int) -> dict[str, Any] | None:
    for d in devices or []:
        if str(d.get("devId")) != str(dev_id):
            continue
        for p in (d.get("deviceInfo") or {}).get("ports") or []:
            try:
                if int(p.get("port")) == int(port):
                    return p
            except (TypeError, ValueError):
                continue
    return None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def port_matches(live: dict[str, Any], expected: dict[str, Any]) -> bool:
    at_type = expected.get("atType")
    speed = _int(live.get("speak"))
    if at_type == 1:  # Off
        load = _int(live.get("loadState"))
        return speed == 0 or load == 0
    if at_type == 2:  # Manual On
        return speed is not None and speed == expected.get("speed")
    mode = _int(live.get("curMode"))
    return mode is not None and mode == at_type


def start(
    client: "ACInfinityClient",
    dev_id: str,
    port: int,
    expected: dict[str, Any],
    *,
    fmt: str,
    on_done: Callable[[], None] | None = None,
    window: float | None = None,
    poll: float = POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    run_async: bool = True,
) -> str:
    """Begin verifying a write; returns a verify id for ``get_result``."""
    vid = uuid.uuid4().hex[:12]
    window = verify_window_seconds() if window is None else window
    _store(vid, {"status": "pending", "dev_id": dev_id, "port": port, "expected": expected, "format": fmt,
                 "started": time.time()})

    def run() -> None:
        deadline = time.monotonic() + window
        last_live: dict[str, Any] | None = None
        while True:
            try:
                live = _live_port(client.get_devices(), dev_id, port)
            except Exception:  # noqa: BLE001 — keep polling; report at the end
                logger.debug("verify poll failed", exc_info=True)
                live = None
            if live is not None:
                last_live = live
                if port_matches(live, expected):
                    _store(vid, {**(get_result(vid) or {}), "status": "applied", "live": _summary(live)})
                    break
            if time.monotonic() + poll > deadline:
                _store(vid, {**(get_result(vid) or {}), "status": "not_applied",
                             "live": _summary(last_live) if last_live else None,
                             "hint": _hint(fmt)})
                break
            sleep(poll)
        if on_done:
            try:
                on_done()
            except Exception:  # noqa: BLE001
                logger.debug("verify on_done failed", exc_info=True)

    if run_async:
        threading.Thread(target=run, name=f"verify-{vid}", daemon=True).start()
    else:
        run()
    return vid


def _summary(live: dict[str, Any]) -> dict[str, Any]:
    return {"speed": live.get("speak"), "load_state": live.get("loadState"), "mode": live.get("curMode")}


def _hint(fmt: str) -> str:
    other = "form" if fmt == "query" else "query"
    return (
        "The cloud accepted the command but the controller has not applied it. "
        f"You can try the other write format (ACINFINITY_WRITE_FORMAT={other})."
    )
