"""Write operations for AC Infinity ports and automation programs."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.client import ACInfinityClient

# atType integer codes from AC Infinity app (jadx qv0.java switch on atType)
AT_TYPE_OFF = 1        # port disabled — uses offSpead
AT_TYPE_ON = 2         # manual on — uses onSpead
AT_TYPE_AUTO = 3       # temperature/humidity trigger
AT_TYPE_TIMER_ON = 4   # run for duration then off — uses acitveTimerOn (API typo)
AT_TYPE_TIMER_OFF = 5  # stay off for duration then on — uses acitveTimerOff (API typo)
AT_TYPE_CYCLE = 6      # cycle on/off — uses activeCycleOn, activeCycleOff
AT_TYPE_SCHEDULE = 7   # scheduled window — uses schedStartTime, schedEndtTime (API typo)
AT_TYPE_VPD = 8        # VPD-based trigger — uses targetVpd (×100)

_AT_TYPE_TO_MODE: dict[int, str] = {
    AT_TYPE_OFF: "off",
    AT_TYPE_ON: "manual",
    AT_TYPE_AUTO: "auto",
    AT_TYPE_TIMER_ON: "timer",
    AT_TYPE_TIMER_OFF: "timer",
    AT_TYPE_CYCLE: "cycle",
    AT_TYPE_SCHEDULE: "schedule",
    AT_TYPE_VPD: "vpd",
}

_RATE_LIMIT_SECS = 1.5
_last_write_ts: float = float("-inf")  # sentinel: never written

# Valid ranges — clamp before sending to AC Infinity API
_SPEED_MIN, _SPEED_MAX = 0, 10
_VPD_MIN_KPA, _VPD_MAX_KPA = 0.1, 3.0
_MINS_MIN, _MINS_MAX = 1, 1439  # minutes from midnight / duration


def _clamp(value: int | float, lo: int | float, hi: int | float) -> int | float:
    return max(lo, min(hi, value))


class RateLimitError(Exception):
    pass


class ControlError(Exception):
    pass


def build_mode_payload(
    dev_id: str | int,
    port: int,
    current: dict[str, Any],
    changes: dict[str, Any],
) -> dict[str, Any]:
    """Build complete addDevMode payload by merging changes onto current settings.

    Always called after read_port_settings (read-before-write pattern).
    Preserves AC Infinity API field name typos in output keys.
    VPD target is stored ×100 in the API (1.2 kPa → 120).
    Schedule times are minutes from midnight (0-1439).
    """
    merged = {**current, **changes}
    mode = str(merged.get("mode", "manual"))

    base: dict[str, Any] = {
        "devId": str(dev_id),
        "port": int(port),
    }

    def _speed(key: str, default: int = 5) -> int:
        try:
            return int(_clamp(int(merged.get(key, default)), _SPEED_MIN, _SPEED_MAX))
        except (TypeError, ValueError):
            raise ControlError(f"Invalid value for '{key}': expected a number 0–10")

    def _mins(key: str, default: int = 60) -> int:
        try:
            return int(_clamp(int(merged.get(key, default)), _MINS_MIN, _MINS_MAX))
        except (TypeError, ValueError):
            raise ControlError(f"Invalid value for '{key}': expected a number")

    if mode == "off":
        return {**base, "atType": AT_TYPE_OFF, "offSpead": 0, "onSpead": 0}

    if mode == "manual":
        state = merged.get("state", True)
        speed = _speed("speed")
        if state:
            return {**base, "atType": AT_TYPE_ON, "onSpead": speed, "offSpead": 0}
        return {**base, "atType": AT_TYPE_OFF, "offSpead": 0, "onSpead": 0}

    if mode == "vpd":
        try:
            vpd_raw = float(merged.get("vpd_target", 1.2))
        except (TypeError, ValueError):
            raise ControlError("Invalid value for 'vpd_target': expected a number 0.1–3.0")
        vpd_clamped = float(_clamp(vpd_raw, _VPD_MIN_KPA, _VPD_MAX_KPA))
        return {
            **base,
            "atType": AT_TYPE_VPD,
            "targetVpd": int(round(vpd_clamped * 100)),
            "targetVpdSwitch": 1,
            "onSpead": _speed("on_speed", 8),
            "offSpead": _speed("off_speed", 3),
        }

    if mode == "cycle":
        return {
            **base,
            "atType": AT_TYPE_CYCLE,
            "activeCycleOn": _mins("cycle_on_mins", 15),
            "activeCycleOff": _mins("cycle_off_mins", 45),
            "onSpead": _speed("on_speed", 7),
            "offSpead": _speed("off_speed", 0),
        }

    if mode == "schedule":
        return {
            **base,
            "atType": AT_TYPE_SCHEDULE,
            "schedStartTime": _mins("schedule_begin_mins", 480),
            "schedEndtTime": _mins("schedule_end_mins", 1200),  # API typo
            "onSpead": _speed("on_speed", int(merged.get("speed", 7))),
            "offSpead": _speed("off_speed", 2),
        }

    if mode == "timer":
        return {
            **base,
            "atType": AT_TYPE_TIMER_ON,
            "acitveTimerOn": _mins("timer_mins", 60),  # API typo
            "onSpead": _speed("speed", 7),
            "offSpead": 0,
        }

    raise ControlError(f"Unknown mode: {mode!r}")


def normalize_port_settings(raw_list: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize getdevModeSettingList data list into UI-friendly settings dict.

    API field names preserve AC Infinity typos:
      offSpead / onSpead (not Speed)
      acitveTimerOn / acitveTimerOff (not activeTimer)
      schedEndtTime (not schedEndTime)
      targetVpd is ×100 (e.g. 120 = 1.20 kPa)
    """
    defaults: dict[str, Any] = {
        "mode": "manual",
        "state": True,
        "speed": 5,
        "on_speed": 5,
        "off_speed": 0,
        "vpd_target": 1.2,
        "cycle_on_mins": 15,
        "cycle_off_mins": 45,
        "schedule_begin_mins": 480,
        "schedule_end_mins": 1200,
        "timer_mins": 60,
    }
    if not raw_list:
        return defaults

    raw = raw_list[0] if isinstance(raw_list, list) else raw_list
    at_type = int(raw.get("atType") or raw.get("modeType") or AT_TYPE_ON)
    mode = _AT_TYPE_TO_MODE.get(at_type, "manual")

    return {
        "mode": mode,
        "state": bool(int(raw.get("loadState") or 1)),
        "speed": int(raw.get("speak") or raw.get("onSpead") or defaults["speed"]),
        "on_speed": int(raw.get("onSpead") or defaults["on_speed"]),
        "off_speed": int(raw.get("offSpead") or defaults["off_speed"]),
        "vpd_target": round(int(raw.get("targetVpd") or 120) / 100.0, 2),
        "cycle_on_mins": int(raw.get("activeCycleOn") or defaults["cycle_on_mins"]),
        "cycle_off_mins": int(raw.get("activeCycleOff") or defaults["cycle_off_mins"]),
        "schedule_begin_mins": int(raw.get("schedStartTime") or defaults["schedule_begin_mins"]),
        "schedule_end_mins": int(raw.get("schedEndtTime") or defaults["schedule_end_mins"]),
        "timer_mins": int(raw.get("acitveTimerOn") or raw.get("acitveTimerOff") or defaults["timer_mins"]),
    }


def read_port_settings(
    client: "ACInfinityClient", dev_id: str, port: int
) -> dict[str, Any]:
    """Fetch current port mode settings. Always called before any write (read-before-write)."""
    raw = client.get_dev_mode_setting_list(dev_id, port)
    if raw and isinstance(raw, dict) and raw.get("code") == 200:
        data = raw.get("data") or []
        if isinstance(data, dict):
            data = [data]
        return normalize_port_settings(data)
    return normalize_port_settings([])


def write_port_control(
    client: "ACInfinityClient",
    dev_id: str,
    port: int,
    changes: dict[str, Any],
) -> None:
    """Apply port control changes. Enforces rate limit and read-before-write.

    Raises:
        RateLimitError: if called again within 1.5s
        ControlError: if API rejects the command
    """
    _rate_limit()
    current = read_port_settings(client, dev_id, port)
    payload = build_mode_payload(dev_id, port, current, changes)
    result = client.set_port_mode(dev_id, port, payload)
    if result is None:
        raise ControlError("Could not reach AC Infinity — check your connection")
    if not isinstance(result, dict):
        return
    code = result.get("code")
    if code == 999999:
        raise ControlError(
            "Port is under active automation — disable it in the AC Infinity app first"
        )
    if code is not None and code != 200:
        msg = str(result.get("msg") or "")
        raise ControlError(f"AC Infinity rejected the command: {msg}" if msg else "Command failed")


def normalize_automations(raw_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group flat getGroups entries by advName → user-visible automation list.

    The API returns one entry per port-speed config. Multiple entries with the same
    advName represent one logical automation; we take the first entry's advId.
    grouptDevType is a bitmask: bit 0 = port 1, bit 1 = port 2, etc.
    """
    seen: dict[str, dict[str, Any]] = {}
    for item in raw_list:
        name = str(item.get("advName") or "Unnamed")
        if name not in seen:
            bitmask = int(item.get("grouptDevType") or 0)
            ports = [i + 1 for i in range(8) if bitmask & (1 << i)]
            seen[name] = {
                "adv_id": str(item.get("advId") or ""),
                "name": name,
                "is_on": bool(item.get("isOn") or item.get("runState")),
                "ports": ports,
                "on_speed": int(item.get("onSpeed") or 0),
            }
    return list(seen.values())


def get_automations(client: "ACInfinityClient", dev_id: str) -> list[dict[str, Any]]:
    """Fetch and normalize named automation programs for a controller."""
    return normalize_automations(client.get_automations_raw(dev_id))


def _reset_rate_limit() -> None:
    """Reset rate limit state. Test helper only."""
    global _last_write_ts
    _last_write_ts = float("-inf")


def _rate_limit() -> None:
    """Enforce 1.5s minimum between writes. Raises RateLimitError if too fast."""
    global _last_write_ts
    elapsed = time.monotonic() - _last_write_ts
    if elapsed < _RATE_LIMIT_SECS:
        raise RateLimitError(
            f"Wait {_RATE_LIMIT_SECS - elapsed:.1f}s before sending another command"
        )
    _last_write_ts = time.monotonic()
