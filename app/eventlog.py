"""Decode the controller's own event log (``log/logdataByAll`` — the app's "Logs" tab).

Tables follow the app's log builders as documented by misterboe/acinfinity-mcp
(docs/api/history.md; ``NetLog.toLog04``): logType 2 = alert, 3 = AI, 4 = mode / automation
info, 5 = controller notice. Unknown types are shown, never dropped.
"""
from __future__ import annotations

from typing import Any

MODE_NAMES = {
    1: "Off", 2: "On", 3: "Auto", 4: "Timer to on", 5: "Timer to off",
    6: "Cycle", 7: "Schedule", 8: "VPD",
}

AI_REASONS = {
    1: "raise temperature", 2: "lower temperature", 3: "raise humidity", 4: "lower humidity",
    5: "raise VPD", 6: "lower VPD", 7: "manage CO₂", 8: "improve efficiency",
    9: "raise temperature and humidity", 10: "lower temperature and humidity",
    11: "raise temperature / lower humidity", 12: "lower temperature / raise humidity",
}

AI_USER_ACTIONS = {
    1: "AI target range updated", 2: "AI schedule updated", 3: "Light schedule updated",
    4: "Port device type changed", 5: "Port device added or removed", 6: "Sensor connection changed",
    7: "Dynamic light schedule updated",
}

AI_MODE_EVENTS = {
    0: "AI paused", 1: "AI started", 2: "AI resumed", 3: "AI deleted",
    4: "Tent work mode on", 5: "Tent work mode ended",
    6: "Unfavorable environment", 7: "Extreme conditions",
    8: "Unfavorable environment", 9: "Extreme conditions",
    10: "Humidity above 90%", 11: "Clip fan setting changed",
    16: "Night mode on", 17: "Night mode off",
}

CONTROLLER_NOTICES = {
    0: "CO₂ above 5000 ppm — devices paused",
    1: "Power protection shut off an outlet",
    2: "Low water detected",
    3: "Built-in clip fan disconnected",
    4: "Built-in fan disconnected",
    5: "Built-in grow light disconnected",
    6: "Built-in sensor disconnected",
    7: "Carbon filter alert",
}

_ALARM_FLAGS = [
    ("isActivateAlarmHightemp", "High temperature alarm triggered"),
    ("isActivateAlarmLowtemp", "Low temperature alarm triggered"),
    ("isActivateAlarmHighhumi", "High humidity alarm triggered"),
    ("isActivateAlarmLowhumi", "Low humidity alarm triggered"),
    ("isDeactivateAlarmHighTemp", "High temperature alarm cleared"),
    ("isDeactivateAlarmLowYemp", "Low temperature alarm cleared"),  # API typo
    ("isDeactivateAlarmHighHumi", "High humidity alarm cleared"),
    ("isDeactivateAlarmLowHumi", "Low humidity alarm cleared"),
]


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def event_time(raw: dict[str, Any]) -> int:
    for key in ("logTime", "createTime"):
        t = _int(raw.get(key))
        if t:
            return t // 1000 if t > 10_000_000_000 else t
    return 0


def event_port(raw: dict[str, Any]) -> int | None:
    p = _int(raw.get("portSelection")) or _int(raw.get("portIndex"))
    return p or None


def _where(port: int | None) -> str:
    return f"port {port}" if port else "controller"


def _mode_detail(raw: dict[str, Any], mode: int | None) -> str:
    on, off = _int(raw.get("fanSpeedOn")), _int(raw.get("fanSpeedOff"))
    if mode == 2 and on:
        return f" (speed {on})"
    if mode == 6 and (raw.get("cycleOn") or raw.get("cycleOff")):
        return f" ({raw.get('cycleOn')} min on / {raw.get('cycleOff')} min off)"
    if mode in (4, 5) and (raw.get("timerOn") or raw.get("timerOff")):
        return f" ({raw.get('timerOn') or raw.get('timerOff')} min)"
    if mode in (3, 8) and on is not None and off is not None and (on or off):
        return f" (speed {off}–{on})"
    return ""


def describe(raw: dict[str, Any]) -> dict[str, Any]:
    """One raw log row → ``{time, port, category, text}``."""
    log_type, business = _int(raw.get("logType")), _int(raw.get("businessType"))
    port = event_port(raw)
    where = _where(port)

    if log_type == 3 and business == 1:
        trend = {1: "decreased", 2: "increased"}.get(_int(raw.get("mlVariationTrend")), "adjusted")
        reason = AI_REASONS.get(_int(raw.get("pauseReason")))
        level = _int(raw.get("mlVariation"))
        text = f"AI {trend} {where}" + (f" (level {level})" if level else "") + (f" to {reason}" if reason else "")
        category = "ai"
    elif log_type == 3 and business == 2:
        kind = _int(raw.get("mlVariationType"))
        text = AI_USER_ACTIONS.get(kind, f"AI setting changed (type {kind})") if kind is not None else "AI setting changed"
        category = "ai"
    elif log_type == 3 and business == 3:
        kind = _int(raw.get("mlVariationType"))
        text = AI_MODE_EVENTS.get(kind, f"AI event (type {kind})") if kind is not None else "AI event"
        category = "ai"
    elif log_type == 2:
        flags = [label for key, label in _ALARM_FLAGS if _int(raw.get(key))]
        name = raw.get("advanceName")
        text = "; ".join(flags) if flags else "Alarm"
        if name:
            text += f" — {name}"
        category = "alert"
    elif log_type == 4:
        mode = _int(raw.get("currentMode"))
        name = raw.get("advanceName")
        if name:
            text = f"Automation “{name}” ran on {where}"
        else:
            text = f"{where[0].upper() + where[1:]} mode: {MODE_NAMES.get(mode or -1, f'mode {mode}')}{_mode_detail(raw, mode)}"
        category = "mode"
    elif log_type == 5:
        text = CONTROLLER_NOTICES.get(business if business is not None else -1, f"Controller notice (type {business})")
        category = "notice"
    else:
        text = f"Unrecognized event (type {log_type}/{business})"
        category = "unknown"
    return {"time": event_time(raw), "port": port, "category": category, "text": text,
            "id": raw.get("id"), "log_type": log_type, "business_type": business}


def fetch_events(client: Any, dev_id: str, *, now: int, hours: float = 24.0, limit: int = 200,
                 page_size: int = 200, max_pages: int = 5) -> dict[str, Any]:
    """Page through ``logdataByAll`` (newest first) until the window or ``limit`` is covered."""
    older = int(now - hours * 3600)
    events: list[dict[str, Any]] = []
    cursor: Any = 0
    truncated = False
    error = None
    for _ in range(max_pages):
        data = client.event_log_page(dev_id, now, older, cursor=cursor, page_size=page_size)
        if data is None:
            error = getattr(client, "last_request_error", None) or "Could not load activity from AC Infinity"
            break
        rows = [r for r in (data.get("rows") or []) if isinstance(r, dict)]
        for r in rows:
            ev = describe(r)
            if ev["time"] and ev["time"] < older:
                continue
            events.append(ev)
        if len(events) >= limit:
            truncated = True
            break
        if len(rows) < page_size or not rows or rows[-1].get("id") in (None, cursor):
            break
        cursor = rows[-1].get("id")
        if event_time(rows[-1]) and event_time(rows[-1]) < older:
            break
    events.sort(key=lambda e: e["time"], reverse=True)
    return {"events": events[:limit], "truncated": truncated or len(events) > limit, "error": error,
            "window_hours": hours}
