"""Write operations for AC Infinity ports and automation programs."""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.client import ACInfinityClient

logger = logging.getLogger(__name__)

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
_write_lock = threading.Lock()  # process-wide: spacing holds across concurrent requests

# ── Write formats ───────────────────────────────────────────────────
# "query": acdash's original recipe — the COMPLETE settings record in the query string,
#          nested devSetting as JSON, bools as "true"/"false" (dalinicus HA client).
# "form":  what the form-body clients send (ober37 ac-infinity-mcp, keithah homebridge,
#          decompiled app 2.0.8 per HA #157): form-urlencoded body, the app's field set only
#          (no devSetting / read-only status fields), bools 0/1, no modeSetid.
WRITE_FORMATS = ("query", "form")
DEFAULT_WRITE_FORMAT = "query"
LEGACY_DEV_TYPES = frozenset({11})

# dalinicus DeviceControlKey (minus devSetting) ∪ keithah's captured app payload.
APP_WRITE_FIELDS: frozenset[str] = frozenset("""
devId externalPort modeType masterPort surplus onSpead offSpead onSelfSpead atType
powerState power loadState loadType speak abnormalState toward schedStartTime schedEndtTime
acitveTimerOn acitveTimerOff activeCycleOn activeCycleOff activeHtVpd activeHtVpdNums
activeLtVpd activeLtVpdNums vpdstatus vpdnums vpdSettingMode targetVpd targetVpdSwitch
isUpdateVpdNums devHt activeHt devLt activeLt temperature targetTemp targetTSwitch insideTemp
outsideTemp devHtf devLtf temperatureF targetTempF devHh activeHh devLh activeLh humidity
targetHumi targetHumiSwitch photocellSwitch trend tTrend hTrend insideTrend outsideTrend unit
ecOrTds ecUnit tdsUnit ecTdsSettingMode ecTdsAccuracy ecTdsTargetSwitch ecTdsTargetValueEcUs
ecTdsTargetValueEcMs ecTdsTargetValueTdsPpm ecTdsTargetValueTdsPpt ecTdsHighSwitch
ecTdsHighValueEcUs ecTdsHighValueEcMs ecTdsHighValueTdsPpm ecTdsHighValueTdsPpt
ecTdsLowSwitchEc ecTdsLowSwitchTds ecTdsLowValueEcUs ecTdsLowValueEcMs ecTdsLowValueTdsPpm
ecTdsLowValueTdsPpt phSettingMode phAccuracy phTargetSwitch phTargetValue phHighSwitch
phHighValue phLowSwitch phLowValue moistureSettingMode moistureAccuracy moistureTargetSwitch
moistureTargetValue moistureHighSwitch moistureHighValue moistureLowSwitch moistureLowValue
waterLevelSettingMode waterLevelAccuracy waterLevelTargetSwitch waterLevelTargetValue
waterLevelHighSwitch waterLevelHighValue waterLevelLowSwitch waterLevelLowValue
waterTempSettingMode waterTempAccuracy waterTempTargetSwitch waterTempTargetValue
waterTempHighSwitch waterTempHighValue waterTempLowSwitch waterTempLowValue
waterTempTargetValueF waterTempHighValueF waterTempLowValueF isOpenAutomation settingMode
onlyUpdateSpeed co2FanHighSwitch co2FanHighValue co2LowSwitch co2LowValue devMacAddr
insidePort outsidePort insideType outsideType settingModeAi vpdSettingModeAi targetTempFAi
""".split())

# Fields the app always sends; when the read omits them, send these instead of 0
# (0 means "port 0 / sensor type 0" and is rejected — HA #157).
FORM_SENTINELS: dict[str, Any] = {
    "insidePort": 255,
    "outsidePort": 255,
    "insideType": 15,
    "outsideType": 15,
    "settingModeAi": 1,
    "vpdSettingModeAi": 1,
    "targetTempFAi": 32,
    "devMacAddr": "",
    "schedStartTime": 65535,
    "schedEndtTime": 65535,
}

# Overlay keys that are acdash bookkeeping, not API fields.
_NON_API_OVERLAY_KEYS = frozenset({"port"})


def get_write_format() -> str:
    """``ACINFINITY_WRITE_FORMAT`` (``query`` default | ``form``); invalid values warn → default."""
    raw = (os.environ.get("ACINFINITY_WRITE_FORMAT") or "").strip().lower()
    if not raw:
        return DEFAULT_WRITE_FORMAT
    if raw not in WRITE_FORMATS:
        logger.warning("Invalid ACINFINITY_WRITE_FORMAT=%r; using %r", raw, DEFAULT_WRITE_FORMAT)
        return DEFAULT_WRITE_FORMAT
    return raw

# Valid ranges — clamp before sending to AC Infinity API
_SPEED_MIN, _SPEED_MAX = 0, 10
_VPD_MIN_KPA, _VPD_MAX_KPA = 0.1, 3.0
_MINS_MIN, _MINS_MAX = 1, 1439  # minutes from midnight / duration
_TEMP_MIN_C, _TEMP_MAX_C = 0, 90       # devHt/devLt range (live API shows 90/194F cap)
_TEMP_MIN_F, _TEMP_MAX_F = 32, 194     # devHtf/devLtf — same bounds in °F
_HUMID_MIN, _HUMID_MAX = 0, 100


def _clamp(value: int | float, lo: int | float, hi: int | float) -> int | float:
    return max(lo, min(hi, value))


def _raw_int(raw: dict[str, Any], *keys: str, default: int) -> int:
    """Return int of the first non-None value found in raw for the given keys.

    Uses explicit None check so 0 is preserved correctly — unlike `raw.get(k) or default`
    which treats 0 as falsy and returns the default instead.
    """
    for k in keys:
        v = raw.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return default


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
    Auto mode thresholds: devHt/devLt raw °C, devHh/devLh raw % (no ×100); devHtf/devLtf °F sent alongside.
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
        # Preserve the timer variant: atType=5 (stay off, then on) must not be
        # silently rewritten as atType=4 (run, then off) on an unchanged save.
        if str(merged.get("timer_variant", "on")) == "off":
            return {
                **base,
                "atType": AT_TYPE_TIMER_OFF,
                "acitveTimerOff": _mins("timer_mins", 60),  # API typo
                "onSpead": _speed("speed", 7),
                "offSpead": 0,
            }
        return {
            **base,
            "atType": AT_TYPE_TIMER_ON,
            "acitveTimerOn": _mins("timer_mins", 60),  # API typo
            "onSpead": _speed("speed", 7),
            "offSpead": 0,
        }

    if mode == "auto":
        def _flag(key: str) -> int:
            return 1 if merged.get(key) else 0

        def _temp_c(key: str, default: float) -> float:
            try:
                v = float(merged.get(key, default))
                if not math.isfinite(v):  # bare NaN/Infinity is valid JSON to json.loads
                    raise ValueError
                return float(_clamp(v, _TEMP_MIN_C, _TEMP_MAX_C))
            except (TypeError, ValueError):
                raise ControlError(f"Invalid value for '{key}': expected a temperature in °C")

        def _humid(key: str, default: int) -> int:
            try:
                return int(_clamp(int(merged.get(key, default)), _HUMID_MIN, _HUMID_MAX))
            except (TypeError, ValueError):
                raise ControlError(f"Invalid value for '{key}': expected a number 0–100")

        active = {
            "activeHt": _flag("auto_high_temp_enabled"),
            "activeLt": _flag("auto_low_temp_enabled"),
            "activeHh": _flag("auto_high_humidity_enabled"),
            "activeLh": _flag("auto_low_humidity_enabled"),
        }
        if not any(active.values()):
            raise ControlError("Enable at least one trigger (temperature or humidity)")

        def _temp_pair(c_key: str, f_key: str, default_c: float) -> tuple[int, int]:
            """(devHt, devHtf) pair. An explicitly sent °F wins (exact round-trip for
            °F-mode users); otherwise derive °F from °C — but keep the device-stored
            °F when the °C value is unchanged, so a no-op °C-mode save doesn't drift
            the app's °F display by ±1 (e.g. stored 27°C/80°F would re-derive as 81°F).
            """
            if f_key in changes:
                try:
                    fv = float(changes[f_key])
                    if not math.isfinite(fv):
                        raise ValueError
                    f = int(round(_clamp(fv, _TEMP_MIN_F, _TEMP_MAX_F)))
                except (TypeError, ValueError):
                    raise ControlError(f"Invalid value for '{f_key}': expected a temperature in °F")
                return int(round((f - 32) * 5 / 9)), f
            c = _temp_c(c_key, default_c)
            cur_c, cur_f = current.get(c_key), current.get(f_key)
            if cur_c is not None and cur_f is not None and int(round(c)) == int(cur_c):
                return int(round(c)), int(cur_f)
            return int(round(c)), int(round(c * 9 / 5 + 32))

        ht_c, ht_f = _temp_pair("auto_high_temp_c", "auto_high_temp_f", 32.0)
        lt_c, lt_f = _temp_pair("auto_low_temp_c", "auto_low_temp_f", 0.0)
        return {
            **base,
            "atType": AT_TYPE_AUTO,
            **active,
            # API expects both °C and °F variants (dalinicus HA integration sends both)
            "devHt": ht_c,
            "devHtf": ht_f,
            "devLt": lt_c,
            "devLtf": lt_f,
            "devHh": _humid("auto_high_humidity", 75),
            "devLh": _humid("auto_low_humidity", 40),
            "onSpead": _speed("on_speed", 5),
            "offSpead": _speed("off_speed", 0),
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
        "timer_variant": "on",  # "on"=atType 4 (run then off), "off"=atType 5 (wait then on)
        # Auto (temp/humidity trigger) mode — atType=3
        "auto_high_temp_enabled": False,
        "auto_low_temp_enabled": False,
        "auto_high_humidity_enabled": False,
        "auto_low_humidity_enabled": False,
        "auto_high_temp_c": 32,
        "auto_high_temp_f": 90,
        "auto_low_temp_c": 0,
        "auto_low_temp_f": 32,
        "auto_high_humidity": 75,
        "auto_low_humidity": 40,
    }
    if not raw_list:
        return defaults

    raw = raw_list[0] if isinstance(raw_list, list) else raw_list

    # Bug fix: use explicit None checks — 0 is a valid atType/loadState/speed value
    # and must not be swallowed by Python's falsy `or` short-circuit.
    at_raw = raw.get("atType")
    mt_raw = raw.get("modeType")
    if at_raw is not None:
        try:
            at_type = int(at_raw)
        except (TypeError, ValueError):
            at_type = AT_TYPE_ON
    elif mt_raw is not None:
        try:
            at_type = int(mt_raw)
        except (TypeError, ValueError):
            at_type = AT_TYPE_ON
    else:
        at_type = AT_TYPE_ON

    mode = _AT_TYPE_TO_MODE.get(at_type, "manual")

    # loadState=0 means OFF — must not be treated as falsy
    load_raw = raw.get("loadState")
    state = bool(int(load_raw)) if load_raw is not None else True

    return {
        "mode": mode,
        "state": state,
        # speed fields: 0 is a valid speed (port off) — use _raw_int, not `or`
        "speed":    _raw_int(raw, "speak", "onSpead", default=defaults["speed"]),
        "on_speed": _raw_int(raw, "onSpead", default=defaults["on_speed"]),
        "off_speed": _raw_int(raw, "offSpead", default=defaults["off_speed"]),
        # VPD ×100 in API
        "vpd_target": round(_raw_int(raw, "targetVpd", default=120) / 100.0, 2),
        # duration fields: 0 is valid for cycle/timer; schedStartTime=0 means midnight
        "cycle_on_mins":      _raw_int(raw, "activeCycleOn",  default=defaults["cycle_on_mins"]),
        "cycle_off_mins":     _raw_int(raw, "activeCycleOff", default=defaults["cycle_off_mins"]),
        "schedule_begin_mins": _raw_int(raw, "schedStartTime", default=defaults["schedule_begin_mins"]),
        "schedule_end_mins":   _raw_int(raw, "schedEndtTime",  default=defaults["schedule_end_mins"]),
        # Timer duration lives in acitveTimerOff for atType=5 (the unused field is 0,
        # not None, so key order matters — pick per variant).
        "timer_mins": (
            _raw_int(raw, "acitveTimerOff", "acitveTimerOn", default=defaults["timer_mins"])
            if at_type == AT_TYPE_TIMER_OFF
            else _raw_int(raw, "acitveTimerOn", "acitveTimerOff", default=defaults["timer_mins"])
        ),
        "timer_variant": "off" if at_type == AT_TYPE_TIMER_OFF else "on",
        # Auto mode triggers — thresholds are raw °C / raw % (confirmed live; no ×100)
        "auto_high_temp_enabled": bool(_raw_int(raw, "activeHt", default=0)),
        "auto_low_temp_enabled": bool(_raw_int(raw, "activeLt", default=0)),
        "auto_high_humidity_enabled": bool(_raw_int(raw, "activeHh", default=0)),
        "auto_low_humidity_enabled": bool(_raw_int(raw, "activeLh", default=0)),
        "auto_high_temp_c": _raw_int(raw, "devHt", default=defaults["auto_high_temp_c"]),
        "auto_high_temp_f": _raw_int(raw, "devHtf", default=defaults["auto_high_temp_f"]),
        "auto_low_temp_c": _raw_int(raw, "devLt", default=defaults["auto_low_temp_c"]),
        "auto_low_temp_f": _raw_int(raw, "devLtf", default=defaults["auto_low_temp_f"]),
        "auto_high_humidity": _raw_int(raw, "devHh", default=defaults["auto_high_humidity"]),
        "auto_low_humidity": _raw_int(raw, "devLh", default=defaults["auto_low_humidity"]),
    }


def read_port_settings(
    client: "ACInfinityClient", dev_id: str, port: int
) -> dict[str, Any]:
    """Fetch current port mode settings. Always called before any write (read-before-write)."""
    raw = client.get_dev_mode_setting_list(dev_id, port)
    return normalize_port_settings([_raw_record(raw)] if _raw_record(raw) else [])


def _raw_record(body: dict[str, Any] | None) -> dict[str, Any]:
    """Pull the full settings record from a getdevModeSettingList response.

    The real API returns ``data`` as a dict; mocks/older shapes may use a 1-item list.
    Returns {} on any failure so callers can detect a missing record.
    """
    if not body or not isinstance(body, dict) or body.get("code") != 200:
        return {}
    data = body.get("data")
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def build_write_payload(
    raw_record: dict[str, Any],
    overlay: dict[str, Any],
    fmt: str = "query",
    *,
    dev_type: int | None = None,
) -> dict[str, Any]:
    """Full read-modify-write payload for addDevMode.

    ``query`` (default, original acdash recipe — mirrors the dalinicus HA client): echo the
    COMPLETE current record with the changed fields overlaid. None → 0, dict/list → compact
    JSON string (e.g. nested ``devSetting``), bool → "true"/"false".

    ``form`` (form-body clients): only ``APP_WRITE_FIELDS`` plus changed keys; nested
    dict/list values dropped; bool → 1/0; None → 0; ``modeSetid`` never sent; missing
    app fields get ``FORM_SENTINELS``; manual On with speed > 0 on a non-legacy controller
    forces ``modeType=2`` (legacy devType 11 keeps the reported value).
    """
    merged = {**(raw_record or {}), **overlay}
    out: dict[str, Any] = {}
    if fmt != "form":
        for k, v in merged.items():
            if v is None:
                out[k] = 0
            elif isinstance(v, (dict, list)):
                out[k] = json.dumps(v, separators=(",", ":"))
            elif isinstance(v, bool):
                out[k] = str(v).lower()
            else:
                out[k] = v
        return out

    keep = APP_WRITE_FIELDS | (set(overlay) - _NON_API_OVERLAY_KEYS)
    for k, v in merged.items():
        if k not in keep or isinstance(v, (dict, list)):
            continue
        if v is None:
            out[k] = 0
        elif isinstance(v, bool):
            out[k] = 1 if v else 0
        else:
            out[k] = v
    for k, v in FORM_SENTINELS.items():
        out.setdefault(k, v)
    out.pop("modeSetid", None)
    try:
        at_type = int(out.get("atType", 0))
        on_speed = int(out.get("onSpead", 0))
    except (TypeError, ValueError):
        at_type, on_speed = 0, 0
    if dev_type not in LEGACY_DEV_TYPES and at_type == AT_TYPE_ON and on_speed > 0:
        out["modeType"] = 2
    return out


def is_noop(raw_record: dict[str, Any], overlay: dict[str, Any]) -> bool:
    """True when every API field in the overlay already equals the stored record."""
    for k, v in overlay.items():
        if k in _NON_API_OVERLAY_KEYS or k == "devId":
            continue
        cur = raw_record.get(k)
        try:
            if cur is None or int(cur) != int(v):
                return False
        except (TypeError, ValueError):
            if str(cur) != str(v):
                return False
    return True


def expected_state(overlay: dict[str, Any]) -> dict[str, Any]:
    """What the controller should report once it applies the write (used by verification)."""
    at_type = int(overlay.get("atType") or 0)
    exp: dict[str, Any] = {"atType": at_type}
    if at_type == AT_TYPE_OFF:
        exp["speed"] = 0
    elif at_type == AT_TYPE_ON:
        exp["speed"] = int(overlay.get("onSpead") or 0)
    return exp


def _port_record(devices: list[dict[str, Any]], dev_id: str, port: int) -> tuple[dict[str, Any], dict[str, Any]]:
    for d in devices or []:
        if str(d.get("devId")) == str(dev_id):
            for p in (d.get("deviceInfo") or {}).get("ports") or []:
                try:
                    if int(p.get("port")) == int(port):
                        return d, p
                except (TypeError, ValueError):
                    continue
            return d, {}
    return {}, {}


def explain_write_failure(
    client: "ACInfinityClient", dev_id: str, port: int, result: dict[str, Any], *, http_status: int | None = None
) -> str:
    """Turn a failed addDevMode response into a specific, actionable message."""
    code = result.get("code")
    msg = str(result.get("msg") or "")
    if (code == 403 or http_status == 403) and "saving failed" in msg.lower():
        return "Too many changes too quickly — wait a few seconds and try again."
    if code == 999999:
        device, port_rec = {}, {}
        try:
            device, port_rec = _port_record(client.get_devices(), dev_id, port)
        except Exception:  # noqa: BLE001 — diagnosis is best-effort
            logger.debug("could not load devices to explain 999999", exc_info=True)
        try:
            if int(port_rec.get("portResistance", -1)) == 65535:
                return "Nothing is plugged into this port."
        except (TypeError, ValueError):
            pass
        if str(device.get("isShare", "0")) == "1":
            return "Shared controllers can't be controlled from this account."
        try:
            if int(port_rec.get("isOpenAutomation") or 0) == 1:
                return "This port is under an Advance Automation — turn it off in the app first."
        except (TypeError, ValueError):
            pass
        return f"AC Infinity rejected the command (code 999999: {msg or 'operation failed'})."
    if msg:
        return f"AC Infinity rejected the command (code {code}): {msg}"
    return f"Command failed (code {code})"


def write_port_control(
    client: "ACInfinityClient",
    dev_id: str,
    port: int,
    changes: dict[str, Any],
    *,
    restore_record: dict[str, Any] | None = None,
    live_port: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply port control changes (or restore a snapshot). Read-before-write, spaced, snapshotted.

    ``live_port`` is the port's live ``devInfoListAll`` entry taken just before the write. A write
    is only skipped as ``no_change`` when the stored settings already match AND the live port
    already shows that state — if the cloud stored a value the controller ignored (HA #166),
    re-sending the same settings must still go out.

    Returns ``{"status": "sent" | "no_change", "format", "expected"}``; verification of the
    live device state is done separately (app.verify).

    Raises:
        RateLimitError: if called again within 1.5s of the previous write
        ControlError: if the pre-write read fails or the API rejects the command
    """
    from app import storage  # local import keeps control importable without a DB

    with _write_lock:
        _rate_limit()
        # Read the FULL current record (not just the normalized view) so the write can
        # echo every field back — addDevMode rejects partial payloads with code 999999.
        raw_body = client.get_dev_mode_setting_list(dev_id, port)
        raw_record = _raw_record(raw_body)
        if not raw_record:
            # Do NOT fall back to defaults and send a partial payload — AC Infinity
            # silently resets omitted fields to 0 on a partial write (and may still
            # return 200), which reads as "the command did nothing." Abort loudly
            # instead so the failure is visible.
            raise ControlError("Could not read current port settings — refusing to send a partial write. Try again.")

        if restore_record is not None:
            overlay = {
                k: v for k, v in restore_record.items()
                if k in APP_WRITE_FIELDS and k not in ("devId", "externalPort") and not isinstance(v, (dict, list))
            }
            overlay["devId"] = str(dev_id)
        else:
            current = normalize_port_settings([raw_record])
            overlay = build_mode_payload(dev_id, port, current, changes)

        fmt = get_write_format()
        expected = expected_state({**raw_record, **overlay})
        if is_noop(raw_record, overlay) and live_port is not None:
            from app.verify import port_matches

            if port_matches(live_port, expected, strict=True):
                return {"status": "no_change", "format": fmt, "expected": expected}

        dev_type = getattr(client, "_dev_types", {}).get(str(dev_id))
        payload = build_write_payload(raw_record, overlay, fmt, dev_type=dev_type)
        storage.save_settings_snapshot(dev_id, port, raw_record, "restore" if restore_record is not None else "write")
        result = client.set_port_mode(dev_id, port, payload, transport=fmt, sign=signing_enabled())

    if result is None:
        raise ControlError("Could not reach AC Infinity — check your connection")
    if not isinstance(result, dict):
        return {"status": "sent", "format": fmt, "expected": expected_state({**raw_record, **overlay})}
    code = result.get("code")
    msg_l = str(result.get("msg") or "").lower()
    if code == 10003 or ("login expired" in msg_l or "login again" in msg_l):
        # The client already renewed the session but deliberately did not resend the write.
        raise ControlError("AC Infinity session had expired and was renewed — please apply the change again.")
    if code is not None and code != 200:
        raise ControlError(explain_write_failure(client, dev_id, port, result))
    return {"status": "sent", "format": fmt, "expected": expected_state({**raw_record, **overlay})}


def signing_enabled() -> bool:
    return (os.environ.get("ACINFINITY_SIGN_WRITES") or "").strip().lower() in ("1", "true", "yes", "on")


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
