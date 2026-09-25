"""Tests for app/control.py."""
import pytest
from app.control import RateLimitError, _rate_limit, _reset_rate_limit


def test_rate_limit_blocks_fast_second_call():
    _reset_rate_limit()
    _rate_limit()  # first call — ok
    with pytest.raises(RateLimitError):
        _rate_limit()  # second call immediately — should raise


def test_rate_limit_allows_after_delay(monkeypatch):
    _reset_rate_limit()
    _rate_limit()
    import app.control as ctrl
    monkeypatch.setattr(ctrl, "_last_write_ts", ctrl._last_write_ts - 2.0)
    _rate_limit()  # should NOT raise — 2s have passed


# ── normalize_port_settings ────────────────────────────────────────
from app.control import (
    normalize_port_settings,
    AT_TYPE_ON, AT_TYPE_OFF, AT_TYPE_VPD,
    AT_TYPE_CYCLE, AT_TYPE_SCHEDULE, AT_TYPE_TIMER_ON,
)


def test_normalize_manual_on():
    raw = [{"atType": AT_TYPE_ON, "onSpead": 7, "offSpead": 0, "speak": 7, "loadState": 1}]
    result = normalize_port_settings(raw)
    assert result["mode"] == "manual"
    assert result["state"] is True
    assert result["speed"] == 7
    assert result["on_speed"] == 7
    assert result["off_speed"] == 0


def test_normalize_off():
    raw = [{"atType": AT_TYPE_OFF, "offSpead": 0, "loadState": 0}]
    result = normalize_port_settings(raw)
    assert result["mode"] == "off"
    # Bug fix: loadState=0 must not be treated as falsy — port is OFF
    assert result["state"] is False


def test_normalize_loadstate_zero_is_off():
    raw = [{"atType": AT_TYPE_ON, "onSpead": 7, "loadState": 0}]
    result = normalize_port_settings(raw)
    assert result["state"] is False, "loadState=0 must return state=False, not True"


def test_normalize_speed_zero_preserved():
    raw = [{"atType": AT_TYPE_OFF, "speak": 0, "onSpead": 0, "offSpead": 0, "loadState": 0}]
    result = normalize_port_settings(raw)
    assert result["speed"] == 0, "speak=0 must not fall through to default 5"
    assert result["on_speed"] == 0, "onSpead=0 must not fall through to default 5"
    assert result["off_speed"] == 0, "offSpead=0 must not fall through to default 0"


def test_normalize_attype_zero_not_treated_as_falsy():
    # atType=0 is not a known mode — should default to manual without crashing
    raw = [{"atType": 0}]
    result = normalize_port_settings(raw)
    # 0 is not in _AT_TYPE_TO_MODE so defaults to "manual" via .get(0, "manual")
    assert result["mode"] == "manual"


def test_normalize_schedule_midnight_start():
    # schedStartTime=0 means midnight — must not be swallowed by falsy-or
    raw = [{"atType": AT_TYPE_SCHEDULE, "schedStartTime": 0, "schedEndtTime": 480}]
    result = normalize_port_settings(raw)
    assert result["schedule_begin_mins"] == 0, "schedStartTime=0 (midnight) must not fall through to 480"
    assert result["schedule_end_mins"] == 480


def test_normalize_auto_mode_returns_auto():
    from app.control import AT_TYPE_AUTO
    raw = [{"atType": AT_TYPE_AUTO, "onSpead": 7, "offSpead": 3, "loadState": 1}]
    result = normalize_port_settings(raw)
    assert result["mode"] == "auto"


def test_normalize_auto_trigger_fields():
    from app.control import AT_TYPE_AUTO
    # Mirrors confirmed live getdevModeSettingList data for an atType=3 port
    raw = [{
        "atType": AT_TYPE_AUTO, "onSpead": 1, "offSpead": 0, "loadState": 0,
        "activeHt": 0, "devHt": 90, "devHtf": 194,
        "activeLt": 0, "devLt": 0, "devLtf": 32,
        "activeHh": 1, "devHh": 62,
        "activeLh": 0, "devLh": 57,
    }]
    result = normalize_port_settings(raw)
    assert result["mode"] == "auto"
    assert result["auto_high_temp_enabled"] is False
    assert result["auto_low_temp_enabled"] is False
    assert result["auto_high_humidity_enabled"] is True
    assert result["auto_low_humidity_enabled"] is False
    assert result["auto_high_temp_c"] == 90
    assert result["auto_high_temp_f"] == 194
    assert result["auto_low_temp_c"] == 0
    assert result["auto_low_temp_f"] == 32
    assert result["auto_high_humidity"] == 62
    assert result["auto_low_humidity"] == 57


def test_normalize_auto_zero_thresholds_preserved():
    from app.control import AT_TYPE_AUTO
    # devLt=0 (0°C) and activeLh=0 are valid values — must not fall to defaults
    raw = [{"atType": AT_TYPE_AUTO, "activeLt": 1, "devLt": 0, "devLh": 0}]
    result = normalize_port_settings(raw)
    assert result["auto_low_temp_enabled"] is True
    assert result["auto_low_temp_c"] == 0
    assert result["auto_low_humidity"] == 0


def test_normalize_empty_includes_auto_defaults():
    result = normalize_port_settings([])
    assert result["auto_high_temp_enabled"] is False
    assert result["auto_high_temp_c"] == 32
    assert result["auto_high_temp_f"] == 90
    assert result["auto_low_temp_c"] == 0
    assert result["auto_low_temp_f"] == 32
    assert result["auto_high_humidity"] == 75
    assert result["auto_low_humidity"] == 40


def test_normalize_vpd():
    raw = [{"atType": AT_TYPE_VPD, "targetVpd": 120, "onSpead": 8, "offSpead": 3}]
    result = normalize_port_settings(raw)
    assert result["mode"] == "vpd"
    assert result["vpd_target"] == 1.2
    assert result["on_speed"] == 8
    assert result["off_speed"] == 3


def test_normalize_cycle():
    raw = [{"atType": AT_TYPE_CYCLE, "activeCycleOn": 15, "activeCycleOff": 45, "onSpead": 7, "offSpead": 0}]
    result = normalize_port_settings(raw)
    assert result["mode"] == "cycle"
    assert result["cycle_on_mins"] == 15
    assert result["cycle_off_mins"] == 45


def test_normalize_schedule():
    raw = [{"atType": AT_TYPE_SCHEDULE, "schedStartTime": 480, "schedEndtTime": 1200, "onSpead": 7, "offSpead": 2}]
    result = normalize_port_settings(raw)
    assert result["mode"] == "schedule"
    assert result["schedule_begin_mins"] == 480
    assert result["schedule_end_mins"] == 1200


def test_normalize_timer():
    raw = [{"atType": AT_TYPE_TIMER_ON, "acitveTimerOn": 90, "onSpead": 7}]
    result = normalize_port_settings(raw)
    assert result["mode"] == "timer"
    assert result["timer_mins"] == 90


def test_normalize_empty_returns_defaults():
    result = normalize_port_settings([])
    assert result["mode"] == "manual"
    assert result["state"] is True
    assert 1 <= result["speed"] <= 10


# ── build_mode_payload ─────────────────────────────────────────────
from app.control import build_mode_payload, ControlError


def _current():
    """Baseline current settings dict (as returned by normalize_port_settings)."""
    return {
        "mode": "manual", "state": True, "speed": 5,
        "on_speed": 5, "off_speed": 0, "vpd_target": 1.2,
        "cycle_on_mins": 15, "cycle_off_mins": 45,
        "schedule_begin_mins": 480, "schedule_end_mins": 1200, "timer_mins": 60,
    }


def test_build_manual_on():
    payload = build_mode_payload("12345", 1, _current(), {"mode": "manual", "state": True, "speed": 8})
    assert payload["atType"] == AT_TYPE_ON
    assert payload["onSpead"] == 8
    assert payload["devId"] == "12345"
    assert payload["port"] == 1


def test_build_manual_off():
    payload = build_mode_payload("12345", 1, _current(), {"mode": "manual", "state": False})
    assert payload["atType"] == AT_TYPE_OFF
    assert payload["offSpead"] == 0


def test_build_off_mode():
    payload = build_mode_payload("12345", 1, _current(), {"mode": "off"})
    assert payload["atType"] == AT_TYPE_OFF


def test_build_vpd():
    payload = build_mode_payload("12345", 1, _current(), {
        "mode": "vpd", "vpd_target": 1.4, "on_speed": 9, "off_speed": 3
    })
    assert payload["atType"] == AT_TYPE_VPD
    assert payload["targetVpd"] == 140  # 1.4 × 100
    assert payload["onSpead"] == 9
    assert payload["offSpead"] == 3
    assert payload["targetVpdSwitch"] == 1


def test_build_cycle():
    payload = build_mode_payload("12345", 1, _current(), {
        "mode": "cycle", "cycle_on_mins": 20, "cycle_off_mins": 40, "on_speed": 7, "off_speed": 0
    })
    assert payload["atType"] == AT_TYPE_CYCLE
    assert payload["activeCycleOn"] == 20
    assert payload["activeCycleOff"] == 40
    assert payload["onSpead"] == 7
    assert payload["offSpead"] == 0


def test_build_schedule():
    payload = build_mode_payload("12345", 1, _current(), {
        "mode": "schedule",
        "schedule_begin_mins": 360,
        "schedule_end_mins": 1080,
        "on_speed": 8,
        "off_speed": 2,
    })
    assert payload["atType"] == AT_TYPE_SCHEDULE
    assert payload["schedStartTime"] == 360
    assert payload["schedEndtTime"] == 1080  # API typo preserved
    assert payload["onSpead"] == 8
    assert payload["offSpead"] == 2


def test_build_timer():
    payload = build_mode_payload("12345", 1, _current(), {
        "mode": "timer", "timer_mins": 120, "speed": 7
    })
    assert payload["atType"] == AT_TYPE_TIMER_ON
    assert payload["acitveTimerOn"] == 120  # API typo preserved
    assert payload["onSpead"] == 7


def test_build_auto():
    from app.control import AT_TYPE_AUTO
    payload = build_mode_payload("12345", 1, _current(), {
        "mode": "auto",
        "auto_high_temp_enabled": True,
        "auto_high_temp_c": 26.6667,   # 80°F from the UI
        "auto_low_temp_enabled": False,
        "auto_low_temp_c": 0,
        "auto_high_humidity_enabled": True,
        "auto_high_humidity": 65,
        "auto_low_humidity_enabled": False,
        "auto_low_humidity": 40,
        "on_speed": 7,
        "off_speed": 2,
    })
    assert payload["atType"] == AT_TYPE_AUTO
    assert payload["activeHt"] == 1
    assert payload["activeLt"] == 0
    assert payload["activeHh"] == 1
    assert payload["activeLh"] == 0
    assert payload["devHt"] == 27          # round(26.6667)
    assert payload["devHtf"] == 80         # round(26.6667 × 9/5 + 32) — °F round-trips exactly
    assert payload["devLt"] == 0
    assert payload["devLtf"] == 32
    assert payload["devHh"] == 65
    assert payload["devLh"] == 40
    assert payload["onSpead"] == 7
    assert payload["offSpead"] == 2


def test_build_auto_no_triggers_raises():
    with pytest.raises(ControlError, match="at least one trigger"):
        build_mode_payload("12345", 1, _current(), {
            "mode": "auto",
            "auto_high_temp_enabled": False,
            "auto_low_temp_enabled": False,
            "auto_high_humidity_enabled": False,
            "auto_low_humidity_enabled": False,
        })


def test_build_auto_clamps_ranges():
    payload = build_mode_payload("12345", 1, _current(), {
        "mode": "auto",
        "auto_high_temp_enabled": True,
        "auto_high_temp_c": 500,        # → clamp 90°C / 194°F (API max, confirmed live)
        "auto_high_humidity_enabled": True,
        "auto_high_humidity": 150,      # → clamp 100
        "on_speed": 99,                 # → clamp 10
    })
    assert payload["devHt"] == 90
    assert payload["devHtf"] == 194
    assert payload["devHh"] == 100
    assert payload["onSpead"] == 10


def test_build_auto_invalid_temp_raises():
    with pytest.raises(ControlError, match="auto_high_temp_c"):
        build_mode_payload("12345", 1, _current(), {
            "mode": "auto",
            "auto_high_temp_enabled": True,
            "auto_high_temp_c": "hot",
        })


def test_build_unknown_mode_raises():
    with pytest.raises(ControlError):
        build_mode_payload("12345", 1, _current(), {"mode": "bogus"})


# ── write_port_control + automation helpers ────────────────────────
from unittest.mock import MagicMock
from app.control import (
    read_port_settings,
    write_port_control,
    normalize_automations,
    get_automations,
)


def _mock_client_with_settings(at_type=AT_TYPE_ON, on_speed=7):
    client = MagicMock()
    client.get_dev_mode_setting_list.return_value = {
        "code": 200,
        "data": [{"atType": at_type, "onSpead": on_speed, "offSpead": 0, "speak": on_speed, "loadState": 1}],
    }
    client.set_port_mode.return_value = {"code": 200, "msg": "success"}
    return client


def test_read_port_settings_returns_normalized():
    client = _mock_client_with_settings(AT_TYPE_ON, 7)
    result = read_port_settings(client, "12345", 1)
    assert result["mode"] == "manual"
    assert result["speed"] == 7
    client.get_dev_mode_setting_list.assert_called_once_with("12345", 1)


def test_write_port_control_calls_set_port_mode():
    client = _mock_client_with_settings()
    _reset_rate_limit()
    write_port_control(client, "12345", 1, {"mode": "manual", "state": True, "speed": 9})
    assert client.set_port_mode.called


def test_write_port_control_raises_on_999999():
    client = _mock_client_with_settings()
    # 999999 is a generic "operation failed" — surface the server's message verbatim,
    # not a misleading "under active automation" claim.
    client.set_port_mode.return_value = {"code": 999999, "msg": "Operation failed, please try again"}
    _reset_rate_limit()
    with pytest.raises(ControlError, match="Operation failed"):
        write_port_control(client, "12345", 1, {"mode": "manual", "state": True, "speed": 5})


def test_write_port_control_sends_full_record_not_partial():
    """The write must echo the COMPLETE current record (with overlay), not a few fields —
    a partial payload is what AC Infinity rejects with 999999."""
    from unittest.mock import MagicMock
    client = MagicMock()
    # A realistic ~partial of the live getdevModeSettingList record
    client.get_dev_mode_setting_list.return_value = {"code": 200, "data": {
        "modeSetid": "999", "devId": "12345", "externalPort": 1,
        "atType": AT_TYPE_ON, "onSpead": 7, "offSpead": 0, "loadState": 1,
        "devHh": 70, "devLh": 40, "co2HighValue": 1200, "phTargetValue": 6,
        "devSetting": {"port": 1, "devLight": 3}, "ipcSetting": None,
    }}
    client.set_port_mode.return_value = {"code": 200, "msg": "success"}
    _reset_rate_limit()
    write_port_control(client, "12345", 1, {"mode": "manual", "state": True, "speed": 9})
    sent = client.set_port_mode.call_args[0][2]   # payload arg
    # carried-over fields the overlay never touches must still be present
    assert sent["co2HighValue"] == 1200
    assert sent["phTargetValue"] == 6
    assert sent["devHh"] == 70
    # overlaid field reflects the change (manual on, speed 9)
    assert sent["atType"] == AT_TYPE_ON
    assert sent["onSpead"] == 9
    # serialization rules: nested dict → JSON string, None → 0
    assert sent["devSetting"] == '{"port":1,"devLight":3}'
    assert sent["ipcSetting"] == 0


def test_build_write_payload_serialization():
    from app.control import build_write_payload
    raw = {"a": 5, "nested": {"x": 1}, "arr": [1, 2], "none_f": None, "flag": True, "keep": "s"}
    out = build_write_payload(raw, {"a": 9})
    assert out["a"] == 9                      # overlay wins
    assert out["nested"] == '{"x":1}'         # dict → compact JSON
    assert out["arr"] == "[1,2]"              # list → compact JSON
    assert out["none_f"] == 0                 # None → 0
    assert out["flag"] == "true"              # bool → lowercase string
    assert out["keep"] == "s"


def test_write_port_control_raises_on_none_response():
    client = _mock_client_with_settings()
    client.set_port_mode.return_value = None
    _reset_rate_limit()
    with pytest.raises(ControlError, match="reach"):
        write_port_control(client, "12345", 1, {"mode": "manual", "state": True, "speed": 5})


def test_write_port_control_refuses_partial_write_on_read_failure():
    """If the pre-write read (getdevModeSettingList) fails or returns a non-200 body,
    write_port_control must abort loudly instead of silently falling back to defaults
    and sending a PARTIAL payload — AC Infinity resets omitted fields to 0 on a partial
    write and can still return 200, so a silent fallback here reads as "did nothing"."""
    client = MagicMock()
    client.get_dev_mode_setting_list.return_value = {"code": 500, "msg": "server hiccup"}
    _reset_rate_limit()
    with pytest.raises(ControlError, match="current port settings"):
        write_port_control(client, "12345", 1, {"mode": "manual", "state": True, "speed": 9})
    client.set_port_mode.assert_not_called()


def test_write_port_control_refuses_partial_write_on_none_read():
    client = MagicMock()
    client.get_dev_mode_setting_list.return_value = None
    _reset_rate_limit()
    with pytest.raises(ControlError, match="current port settings"):
        write_port_control(client, "12345", 1, {"mode": "manual", "state": True, "speed": 9})
    client.set_port_mode.assert_not_called()


def test_normalize_automations_groups_by_name():
    raw = [
        {"advId": "1", "advName": "Night Cycle", "isOn": 1, "grouptDevType": 3, "onSpeed": 7},
        {"advId": "2", "advName": "Night Cycle", "isOn": 1, "grouptDevType": 3, "onSpeed": 7},
        {"advId": "3", "advName": "VPD Boost", "isOn": 0, "grouptDevType": 1, "onSpeed": 8},
    ]
    result = normalize_automations(raw)
    assert len(result) == 2
    names = [a["name"] for a in result]
    assert "Night Cycle" in names
    assert "VPD Boost" in names


def test_normalize_automations_decodes_port_bitmask():
    # grouptDevType bitmask: 3 = 0b011 = ports 1 and 2
    raw = [{"advId": "1", "advName": "Test", "isOn": 1, "grouptDevType": 3, "onSpeed": 5}]
    result = normalize_automations(raw)
    assert set(result[0]["ports"]) == {1, 2}


def test_get_automations_uses_client():
    client = MagicMock()
    client.get_automations_raw.return_value = [
        {"advId": "1", "advName": "Test", "isOn": 1, "grouptDevType": 1, "onSpeed": 5}
    ]
    result = get_automations(client, "12345")
    assert len(result) == 1
    assert result[0]["name"] == "Test"


# ── audit fixes: timer variant + °F preservation + disabled-trigger merge ──
from app.control import AT_TYPE_TIMER_OFF, AT_TYPE_AUTO


def test_normalize_timer_off_variant():
    raw = [{"atType": AT_TYPE_TIMER_OFF, "acitveTimerOn": 0, "acitveTimerOff": 90, "onSpead": 7}]
    result = normalize_port_settings(raw)
    assert result["mode"] == "timer"
    assert result["timer_variant"] == "off"
    # duration must come from acitveTimerOff, not the zeroed acitveTimerOn
    assert result["timer_mins"] == 90


def test_normalize_timer_on_variant():
    raw = [{"atType": AT_TYPE_TIMER_ON, "acitveTimerOn": 45, "acitveTimerOff": 0, "onSpead": 7}]
    result = normalize_port_settings(raw)
    assert result["timer_variant"] == "on"
    assert result["timer_mins"] == 45


def test_build_timer_off_variant_preserved():
    # An unchanged save of an atType=5 port must re-emit atType=5, not flip to 4
    current = {**_current(), "mode": "timer", "timer_variant": "off", "timer_mins": 90, "speed": 7}
    payload = build_mode_payload("12345", 1, current, {"mode": "timer"})
    assert payload["atType"] == AT_TYPE_TIMER_OFF
    assert payload["acitveTimerOff"] == 90
    assert "acitveTimerOn" not in payload


def test_build_timer_on_variant_default():
    payload = build_mode_payload("12345", 1, _current(), {"mode": "timer", "timer_mins": 60})
    assert payload["atType"] == AT_TYPE_TIMER_ON
    assert payload["acitveTimerOn"] == 60


def _auto_current():
    """Current settings for a port already in auto mode (mirrors live 2x4 tent port 4)."""
    return {
        **_current(),
        "mode": "auto",
        "auto_high_temp_enabled": True,
        "auto_low_temp_enabled": False,
        "auto_high_humidity_enabled": False,
        "auto_low_humidity_enabled": False,
        "auto_high_temp_c": 27, "auto_high_temp_f": 80,
        "auto_low_temp_c": 0, "auto_low_temp_f": 32,
        "auto_high_humidity": 56, "auto_low_humidity": 0,
    }


def test_build_auto_f_key_sent_directly():
    # °F-mode UI sends auto_high_temp_f; devHtf must store it exactly, °C derived
    payload = build_mode_payload("12345", 1, _auto_current(), {
        "mode": "auto", "auto_high_temp_enabled": True, "auto_high_temp_f": 81,
    })
    assert payload["devHtf"] == 81
    assert payload["devHt"] == 27  # round((81-32)*5/9) = round(27.2)


def test_build_auto_unchanged_c_preserves_stored_f():
    # °C-mode no-op save: stored pair (27°C, 80°F) must not drift to 81°F
    payload = build_mode_payload("12345", 1, _auto_current(), {
        "mode": "auto", "auto_high_temp_enabled": True, "auto_high_temp_c": 27,
    })
    assert payload["devHt"] == 27
    assert payload["devHtf"] == 80  # preserved, not round(27*9/5+32)=81


def test_build_auto_changed_c_derives_f():
    payload = build_mode_payload("12345", 1, _auto_current(), {
        "mode": "auto", "auto_high_temp_enabled": True, "auto_high_temp_c": 28,
    })
    assert payload["devHt"] == 28
    assert payload["devHtf"] == 82  # round(28*9/5+32) = round(82.4)


def test_build_auto_disabled_triggers_keep_current_thresholds():
    # JS omits thresholds for disabled triggers; merge must keep stored values
    payload = build_mode_payload("12345", 1, _auto_current(), {
        "mode": "auto",
        "auto_high_temp_enabled": True,   # only flags + speeds sent
        "auto_low_temp_enabled": False,
        "auto_high_humidity_enabled": False,
        "auto_low_humidity_enabled": False,
        "on_speed": 3,
    })
    assert payload["devHt"] == 27
    assert payload["devHtf"] == 80   # preserved via unchanged-°C path
    assert payload["devHh"] == 56    # disabled trigger threshold untouched
    assert payload["devLh"] == 0
    assert payload["onSpead"] == 3


def test_build_auto_f_key_clamped():
    payload = build_mode_payload("12345", 1, _auto_current(), {
        "mode": "auto", "auto_high_temp_enabled": True, "auto_high_temp_f": 300,
    })
    assert payload["devHtf"] == 194
    assert payload["devHt"] == 90


def test_build_auto_f_key_invalid_raises():
    with pytest.raises(ControlError, match="auto_high_temp_f"):
        build_mode_payload("12345", 1, _auto_current(), {
            "mode": "auto", "auto_high_temp_enabled": True, "auto_high_temp_f": "hot",
        })


def test_build_auto_fractional_c_same_int_preserves_f():
    # 27.4°C rounds to the same stored int (27) → device behavior unchanged → keep °F 80
    payload = build_mode_payload("12345", 1, _auto_current(), {
        "mode": "auto", "auto_high_temp_enabled": True, "auto_high_temp_c": 27.4,
    })
    assert payload["devHt"] == 27
    assert payload["devHtf"] == 80


def test_build_auto_nan_rejected():
    # bare NaN is valid JSON to json.loads — must 400, not silently clamp to max
    with pytest.raises(ControlError, match="auto_high_temp_f"):
        build_mode_payload("12345", 1, _auto_current(), {
            "mode": "auto", "auto_high_temp_enabled": True, "auto_high_temp_f": float("nan"),
        })
    with pytest.raises(ControlError, match="auto_high_temp_c"):
        build_mode_payload("12345", 1, _auto_current(), {
            "mode": "auto", "auto_high_temp_enabled": True, "auto_high_temp_c": float("inf"),
        })
