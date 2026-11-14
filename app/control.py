"""Write operations for AC Infinity ports and automation programs."""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import asdict, dataclass
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


# °F fields the Android app clamps to >= 32 before every write (jadx yr.resetTemperatureValues).
F_FIELDS_MIN_32 = ("devHtf", "targetTempF", "devLtf", "waterTempTargetValueF", "waterTempHighValueF", "waterTempLowValueF")


@dataclass(frozen=True)
class WriteVariant:
    """Opt-in write knobs for tools/control_experiment.py. The dashboard never passes one, so
    its requests stay byte-identical; each field left at its default changes nothing."""

    name: str = "default"
    only_update_speed: int | None = None   # app sends 1 from the power slider, 0 on mode saves
    clamp_f: bool = False                  # app raises °F fields < 32 to 32
    force_mode_type: int | None = None     # ober37 forces modeType=2 when on-speed > 0
    fmt: str | None = None                 # "query" | "form" override
    sign: bool | None = None               # app 2.0.8 request signing
    app_headers: bool = False              # app sends devType + empty minversion on devType 11
    api_base: str | None = None            # e.g. plain-http base for the write only

    def knobs(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if k != "name" and v not in (None, False)}


def apply_variant(payload: dict[str, Any], variant: "WriteVariant | None") -> dict[str, Any]:
    """Apply a WriteVariant's payload knobs. ``None`` returns the payload unchanged."""
    if variant is None:
        return payload
    out = dict(payload)
    if variant.only_update_speed is not None:
        out["onlyUpdateSpeed"] = int(variant.only_update_speed)
    if variant.clamp_f:
        for key in F_FIELDS_MIN_32:
            v = out.get(key)
            if isinstance(v, int) and not isinstance(v, bool) and v < 32:
                out[key] = 32
    if variant.force_mode_type is not None:
        out["modeType"] = int(variant.force_mode_type)
    return out


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


class UpstreamError(ControlError):
    """AC Infinity could not be reached or did not confirm the change (HTTP 502)."""


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
    variant: WriteVariant | None = None,
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

        fmt = (variant.fmt if variant is not None and variant.fmt else None) or get_write_format()
        sign = variant.sign if variant is not None and variant.sign is not None else signing_enabled()
        expected = expected_state({**raw_record, **overlay})
        if is_noop(raw_record, overlay) and live_port is not None:
            from app.verify import port_matches

            if port_matches(live_port, expected, strict=True):
                return {"status": "no_change", "format": fmt, "expected": expected}

        dev_type = getattr(client, "_dev_types", {}).get(str(dev_id))
        payload = apply_variant(build_write_payload(raw_record, overlay, fmt, dev_type=dev_type), variant)
        storage.save_settings_snapshot(dev_id, port, raw_record, "restore" if restore_record is not None else "write")
        if variant is None:
            result = client.set_port_mode(dev_id, port, payload, transport=fmt, sign=sign)
        else:
            result = client.set_port_mode(dev_id, port, payload, transport=fmt, sign=sign,
                                          app_headers=variant.app_headers, dev_type=dev_type,
                                          api_base=variant.api_base)

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
    """getGroups entries → user-visible automation list, one row per advId.

    Keyed by advId (what toggle/delete act on), so two automations that share a name both
    show and both stay manageable. grouptDevType is a port bitmask: bit 0 = port 1, etc.
    """
    seen: dict[str, dict[str, Any]] = {}
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        adv_id = str(item.get("advId") or "")
        if not adv_id or adv_id in seen:
            continue
        bitmask = _as_int(item.get("grouptDevType"))
        day_bits = _as_int(item.get("switchTime"))
        seen[adv_id] = {
            "adv_id": adv_id,
            "name": str(item.get("advName") or "Unnamed"),
            # isOn = enabled; runState = currently acting (inside its window / trigger met)
            "is_on": bool(_as_int(item.get("isOn"))),
            "running": bool(_as_int(item.get("runState"))),
            "ports": [i + 1 for i in range(8) if bitmask & (1 << i)],
            "on_speed": _as_int(item.get("onSpeed")),
            "off_speed": _as_int(item.get("offSpeed")),
            "mode": AUTOMATION_MODE_NAMES.get(_as_int(item.get("currentMode")), "other"),
            "continuous": bool(day_bits & AUTOMATION_CONTINUOUS),
            "days": [d for d, bit in AUTOMATION_DAY_BITS.items() if day_bits & bit],
            "begin_mins": _as_int(item.get("beginTime")),
            "end_mins": _as_int(item.get("endTime")),
        }
    return list(seen.values())


def _as_int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _read_automations(client: "ACInfinityClient", dev_id: str) -> list[dict[str, Any]]:
    raw = client.get_automations_raw(dev_id)
    if raw is None:
        raise UpstreamError("Could not read the automation list from AC Infinity — try again.")
    return raw


def get_automations(client: "ACInfinityClient", dev_id: str) -> list[dict[str, Any]]:
    """Fetch and normalize named automation programs. Raises UpstreamError if the read fails."""
    return normalize_automations(_read_automations(client, dev_id))


# ── Automation create / enable (app 2.0.0 NetAutomation; verified live on a real controller) ──
# Automation modes are NOT port atTypes: 1 = ON, 2 = OFF (p5.getFormattedModeName).
AUTOMATION_MODE_ON = 1
AUTOMATION_MODE_OFF = 2
AUTOMATION_MODE_NAMES = {1: "on", 2: "off", 3: "cycle", 4: "auto", 6: "vpd"}
# switchTime day byte (el.timeConversion): bit 7 = run continuously (window ignored).
AUTOMATION_CONTINUOUS = 0x80
AUTOMATION_DAY_BITS = {"mon": 1, "tue": 2, "wed": 4, "thu": 8, "fri": 16, "sat": 32, "sun": 64}
AUTOMATION_EVERY_DAY = 0x7F
_DAY_NAMES = {
    **{d: d for d in AUTOMATION_DAY_BITS},
    "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
    "friday": "fri", "saturday": "sat", "sunday": "sun",
}

# Every NetAutomation field the app sends, at the app's neutral defaults.
_AUTOMATION_DEFAULTS: dict[str, Any] = {
    "isDel": "false", "isExist": 1, "templateType": 0, "groupNums": 0, "sortType": 0,
    "subNumber": 0, "subNumberSort": 0, "portType": 0, "portState": 0, "runState": 0,
    "onTimeSwitch": 0, "onTime": 0, "cycleOn": 0, "cycleOff": 0, "setSelect": 0,
    "temperatureFTrans": 0, "humidityTrans": 0, "vpdTrans": 0, "temperatureFBuff": 0,
    "humidityBuff": 0, "vpdBuff": 0, "switchTemperatureFBuff": 0, "switchHumidityBuff": 0,
    "switchVpdBuff": 0, "autoHighTempF": 32, "autoHighTempC": 0, "autoLowTempF": 32,
    "autoLowTempC": 0, "autoHighHumi": 100, "autoLowHumi": 0, "autoHighTempSwitch": 0,
    "autoLowTempSwitch": 0, "autoHighHumiSwitch": 0, "autoLowHumiSwitch": 0, "highVpd": 99,
    "lowVpd": 0, "highVpdSwitch": 0, "lowVpdSwitch": 0, "targetTSwitch": 0,
    "targetHumiSwitch": 0, "targetVpdSwitch": 0, "settingMode": 0, "targetTemp": 0,
    "targetTempF": 32, "targetHumi": 0, "targetVpd": 0, "isOnMinMaxTime": 1, "onMinTime": 0,
    "onMaxTime": 0, "isOpenDoseTime": 0, "onDoseTime": 0, "offDoseTime": 0,
    "photocellSwitch": 0, "dualZoneSwitch": 1, "sensorModeData": "[]", "sensorModeDataNum": 0,
    "fanLevel": 0, "dynamicWindEn": 0, "minLevel": 0, "timerOff": 0, "targetSwitch": 0,
    "triggerSwitch": 0, "triggerValue": 0, "targetValue": 0, "buff": 0, "trans": 0,
}


def day_byte(days: list[str] | None, *, continuous: bool = False) -> int:
    """Build the switchTime byte. ``days`` None/empty = every day."""
    if continuous:
        return AUTOMATION_CONTINUOUS | AUTOMATION_EVERY_DAY
    if not days:
        return AUTOMATION_EVERY_DAY
    out = 0
    for d in days:
        key = _DAY_NAMES.get(d.strip().lower()) if isinstance(d, str) else None
        if key is None:
            raise ValueError(f"Unknown day {d!r} (use mon, tue, … or monday, tuesday, …)")
        out |= AUTOMATION_DAY_BITS[key]
    return out


def build_automation_payload(
    *,
    name: str,
    ports: list[int],
    begin_mins: int,
    end_mins: int,
    mode: int = AUTOMATION_MODE_ON,
    on_speed: int = 5,
    off_speed: int = 0,
    days: list[str] | None = None,
    continuous: bool = False,
    enabled: bool = True,
    dev_version: int = 0,
) -> dict[str, Any]:
    """Full addGroups record for a time-window ON/OFF automation on standard controllers.

    ``begin_mins``/``end_mins`` are minutes of day (0–1439); begin > end crosses midnight
    (e.g. 22:00→06:00). Raises ValueError on bad input.
    """
    name = str(name or "").strip()[:64]
    if not name:
        raise ValueError("Name is required")
    if not ports or any(not 1 <= int(p) <= 8 for p in ports):
        raise ValueError("Pick at least one port (1–8)")
    if mode not in (AUTOMATION_MODE_ON, AUTOMATION_MODE_OFF):
        raise ValueError("Only ON and OFF automations are supported here")
    for label, v in (("Start", begin_mins), ("End", end_mins)):
        if not 0 <= int(v) <= 1439:
            raise ValueError(f"{label} time must be 00:00–23:59")
    if not continuous and int(begin_mins) == int(end_mins):
        raise ValueError("Start and end time can't be the same")
    for label, v in (("On level", on_speed), ("Off level", off_speed)):
        if not 0 <= int(v) <= 10:
            raise ValueError(f"{label} must be 0–10")
    return {
        **_AUTOMATION_DEFAULTS,
        "advName": name,
        "isOn": 1 if enabled else 0,
        "switchTime": day_byte(days, continuous=continuous),
        "beginTime": int(begin_mins),
        "endTime": int(end_mins),
        "grouptDevType": sum(1 << (int(p) - 1) for p in set(ports)),
        "currentMode": mode,
        "onSpeed": int(on_speed),
        "offSpeed": int(off_speed),
        "devVersion": int(dev_version or 0),
    }


# One automation change at a time per process: enable/disable is read-then-FLIP, so two
# overlapping requests (double click, retry) would otherwise flip twice and cancel out.
_automation_lock = threading.Lock()
AUTOMATION_LOCK_WAIT_SECS = 45.0
AUTOMATION_CONFIRM_SECS = 12.0
_POLL_SECS = 2.0
_sleep = time.sleep
_clock = time.monotonic


class _AutomationLock:
    def __enter__(self) -> None:
        if not _automation_lock.acquire(timeout=AUTOMATION_LOCK_WAIT_SECS):
            raise ControlError("Another automation change is still running — try again in a moment.")

    def __exit__(self, *exc: Any) -> None:
        _automation_lock.release()


def _find_automation(client: "ACInfinityClient", dev_id: str, adv_id: str) -> dict[str, Any] | None:
    """The automation with ``adv_id``, or None if a SUCCESSFUL read doesn't list it."""
    return next((a for a in _read_automations(client, dev_id) if str(a.get("advId")) == str(adv_id)), None)


def _check_write(result: Any, what: str) -> None:
    if not isinstance(result, dict):
        raise UpstreamError("Could not reach AC Infinity — refresh the list and check before retrying.")
    if result.get("code") != 200:
        raise ControlError(str(result.get("msg") or f"AC Infinity rejected the {what}."))


def _poll(check: Any, *, wait_secs: float | None, sleep: Any, clock: Any) -> bool:
    """Call ``check()`` until it returns True or ``wait_secs`` of real time pass.

    A failed read inside the loop counts as "not yet" — only the final verdict matters.
    Defaults are looked up at call time (module ``AUTOMATION_CONFIRM_SECS`` / ``_sleep`` /
    ``_clock``) so tests can shorten them.
    """
    sleep = sleep or _sleep
    clock = clock or _clock
    deadline = clock() + (AUTOMATION_CONFIRM_SECS if wait_secs is None else wait_secs)
    while True:
        try:
            if check():
                return True
        except UpstreamError:
            pass
        if clock() >= deadline:
            return False
        sleep(_POLL_SECS)


def set_automation_enabled(
    client: "ACInfinityClient",
    dev_id: str,
    adv_id: str,
    is_on: bool,
    *,
    wait_secs: float | None = None,
    sleep: Any = None,
    clock: Any = None,
) -> str:
    """Enable/disable an automation. The API call FLIPS state, so read first and only flip
    when needed, then confirm. Returns "unchanged" or "changed"; raises ControlError otherwise.
    """
    with _AutomationLock():
        current = _find_automation(client, dev_id, adv_id)
        if current is None:
            raise ControlError("That automation no longer exists — refresh the list.")
        if bool(_as_int(current.get("isOn"))) == is_on:
            return "unchanged"
        _check_write(client.toggle_automation_raw(dev_id, adv_id), "change")

        def switched() -> bool:
            now = _find_automation(client, dev_id, adv_id)
            return now is not None and bool(_as_int(now.get("isOn"))) == is_on

        if not _poll(switched, wait_secs=wait_secs, sleep=sleep, clock=clock):
            raise UpstreamError("AC Infinity accepted the change but didn't confirm it — refresh and check.")
        return "changed"


def delete_automation(
    client: "ACInfinityClient",
    dev_id: str,
    adv_id: str,
    *,
    wait_secs: float | None = None,
    sleep: Any = None,
    clock: Any = None,
) -> str:
    """Delete an automation and confirm it is gone. Returns "deleted" or "already_gone".

    "Gone" is only accepted from a read that succeeded; a failed read raises UpstreamError.
    """
    with _AutomationLock():
        if _find_automation(client, dev_id, adv_id) is None:
            return "already_gone"
        _check_write(client.delete_automation_raw(dev_id, adv_id), "delete")

        def gone() -> bool:
            return _find_automation(client, dev_id, adv_id) is None

        if not _poll(gone, wait_secs=wait_secs, sleep=sleep, clock=clock):
            raise UpstreamError("AC Infinity said deleted but the automation is still listed — refresh and check.")
        return "deleted"


def create_automation(
    client: "ACInfinityClient",
    dev_id: str,
    payload: dict[str, Any],
    *,
    wait_secs: float | None = None,
    sleep: Any = None,
    clock: Any = None,
) -> str:
    """Create an automation from ``build_automation_payload`` output and confirm it is listed.

    Names must be unique per controller (case-insensitive): the list and the user tell
    automations apart by name, and it stops a double-submitted form creating two.
    Returns the new advId.
    """
    name = str(payload.get("advName") or "").strip()
    with _AutomationLock():
        before = _read_automations(client, dev_id)
        if any(str(a.get("advName") or "").strip().lower() == name.lower() for a in before):
            raise ControlError(f'An automation called "{name}" already exists — pick another name.')
        known = {str(a.get("advId")) for a in before}
        _check_write(client.create_automation_raw(dev_id, payload), "new automation")
        found: list[str] = []

        def listed() -> bool:
            for a in _read_automations(client, dev_id):
                adv = str(a.get("advId"))
                if adv not in known and str(a.get("advName") or "").strip() == name:
                    found.append(adv)
                    return True
            return False

        if not _poll(listed, wait_secs=wait_secs, sleep=sleep, clock=clock):
            raise UpstreamError("AC Infinity accepted the automation but it isn't listed yet — refresh and check.")
        return found[0]


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
