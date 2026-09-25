"""fix-port-write-delivery: write formats, transport, failures, spacing, snapshots, verification, signing."""
from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

import app.control as control
from app import client as client_mod
from app import storage, verify
from app.control import (
    APP_WRITE_FIELDS,
    ControlError,
    build_write_payload,
    expected_state,
    get_write_format,
    is_noop,
    write_port_control,
)
from tests.fakes import FakeAPI, make_client


def full_record(**over):
    """A getdevModeSettingList-shaped record with status + nested fields, like the real ~142."""
    rec = {
        "devId": "1", "externalPort": 1, "modeSetid": "ms-1", "atType": 1, "modeType": 0,
        "onSpead": 5, "offSpead": 0, "speak": 0, "loadState": 0, "surplus": 0,
        "activeHt": 0, "devHt": 30, "devHtf": 86, "targetVpd": 120,
        "ecoMode": False, "portResistance": 5000, "firmwareStatus": 3, "zoneId": None,
        "devSetting": {"devId": "1", "tempUnit": 0}, "sensorList": [1, 2],
    }
    rec.update(over)
    return rec


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "h.db"))
    storage.init_db()
    control._reset_rate_limit()
    monkeypatch.delenv("ACINFINITY_WRITE_FORMAT", raising=False)
    monkeypatch.delenv("ACINFINITY_SIGN_WRITES", raising=False)
    yield
    control._reset_rate_limit()


# ── 1. Payload formats ──────────────────────────────────────────────


def test_query_format_unchanged_from_original_recipe():
    raw = full_record()
    overlay = {"devId": "1", "port": 1, "atType": 2, "onSpead": 7, "offSpead": 0}
    out = build_write_payload(raw, overlay)  # default fmt
    assert out == build_write_payload(raw, overlay, "query")
    assert out["devSetting"] == '{"devId":"1","tempUnit":0}'
    assert out["ecoMode"] == "false"
    assert out["zoneId"] == 0
    assert out["modeSetid"] == "ms-1"
    assert out["portResistance"] == 5000  # full record echoed
    assert out["onSpead"] == 7 and out["port"] == 1


def test_form_format_whitelists_and_encodes():
    raw = full_record()
    out = build_write_payload(raw, {"devId": "1", "port": 1, "atType": 2, "onSpead": 7, "offSpead": 0}, "form")
    assert "devSetting" not in out and "sensorList" not in out
    assert "modeSetid" not in out
    assert "portResistance" not in out and "firmwareStatus" not in out
    assert "port" not in out
    assert not any(v in ("true", "false") for v in out.values())
    assert set(out) <= APP_WRITE_FIELDS | {"devId"}
    assert out["insidePort"] == 255 and out["insideType"] == 15 and out["devMacAddr"] == ""
    assert out["onSpead"] == 7 and out["atType"] == 2


def test_form_bool_becomes_int():
    out = build_write_payload(full_record(activeHt=True), {"atType": 3}, "form")
    assert out["activeHt"] == 1


def test_form_mode_type_forced_for_new_generation_on():
    out = build_write_payload(full_record(), {"atType": 2, "onSpead": 6}, "form", dev_type=18)
    assert out["modeType"] == 2


def test_form_mode_type_echoed_for_legacy_devtype_11():
    out = build_write_payload(full_record(modeType=0), {"atType": 2, "onSpead": 6}, "form", dev_type=11)
    assert out["modeType"] == 0


def test_form_mode_type_untouched_when_off():
    out = build_write_payload(full_record(modeType=0), {"atType": 1, "onSpead": 0}, "form", dev_type=18)
    assert out["modeType"] == 0


def test_query_never_forces_mode_type():
    out = build_write_payload(full_record(modeType=0), {"atType": 2, "onSpead": 6}, "query", dev_type=18)
    assert out["modeType"] == 0


@pytest.mark.parametrize("value,expected", [(None, "query"), ("form", "form"), ("QUERY", "query"),
                                            ("banana", "query"), ("  form ", "form")])
def test_get_write_format(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("ACINFINITY_WRITE_FORMAT", raising=False)
    else:
        monkeypatch.setenv("ACINFINITY_WRITE_FORMAT", value)
    assert get_write_format() == expected


def test_invalid_format_logs_warning(monkeypatch, caplog):
    monkeypatch.setenv("ACINFINITY_WRITE_FORMAT", "banana")
    with caplog.at_level("WARNING"):
        get_write_format()
    assert "Invalid ACINFINITY_WRITE_FORMAT" in caplog.text


# ── transport placement ─────────────────────────────────────────────


def _write_via_api(monkeypatch, fmt):
    api = FakeAPI()
    api.queue("/dev/getdevModeSettingList", (200, {"code": 200, "data": full_record()}))
    c = make_client(api)
    if fmt:
        monkeypatch.setenv("ACINFINITY_WRITE_FORMAT", fmt)
    out = write_port_control(c, "1", 1, {"mode": "manual", "state": True, "speed": 7})
    path, query, form, headers = [x for x in api.calls if x[0].endswith("/dev/addDevMode")][-1]
    return out, query, form, headers


def test_query_transport_puts_fields_in_query(monkeypatch):
    out, query, form, _ = _write_via_api(monkeypatch, None)
    assert out["format"] == "query" and out["status"] == "sent"
    assert query["onSpead"] == "7" and query["devSetting"].startswith("{")
    assert form == {}


def test_form_transport_puts_fields_in_body(monkeypatch):
    out, query, form, headers = _write_via_api(monkeypatch, "form")
    assert out["format"] == "form"
    assert query == {}
    assert form["onSpead"] == "7" and "devSetting" not in form and "modeSetid" not in form
    assert "application/x-www-form-urlencoded" in headers["content-type"]


# ── 2. Error mapping, spacing, no-op ────────────────────────────────


def _api_with_write_result(result, *, devices=None, status=200):
    api = FakeAPI()
    api.queue("/dev/getdevModeSettingList", (200, {"code": 200, "data": full_record()}))
    api.queue("/dev/addDevMode", (status, result))
    api.queue("/user/devInfoListAll", (200, {"code": 200, "data": devices or []}))
    return api


def _device(port_resistance=5000, is_share=0, automation=0):
    return [{"devId": "1", "isShare": is_share, "deviceInfo": {"ports": [
        {"port": 1, "portResistance": port_resistance, "isOpenAutomation": automation}]}}]


@pytest.mark.parametrize("devices,needle", [
    (_device(port_resistance=65535), "Nothing is plugged into this port"),
    (_device(is_share=1), "Shared controllers"),
    (_device(automation=1), "Advance Automation"),
    (_device(), "code 999999"),
])
def test_999999_is_explained(devices, needle):
    api = _api_with_write_result({"code": 999999, "msg": "Operation failed"}, devices=devices)
    with pytest.raises(ControlError, match=needle):
        write_port_control(make_client(api), "1", 1, {"mode": "manual", "state": True, "speed": 7})


def test_data_saving_failed_is_too_fast():
    api = _api_with_write_result({"code": 403, "msg": "Data saving failed"}, status=403)
    with pytest.raises(ControlError, match="Too many changes too quickly"):
        write_port_control(make_client(api), "1", 1, {"mode": "manual", "state": True, "speed": 7})


def test_unknown_code_shows_message_and_code():
    api = _api_with_write_result({"code": 100001, "msg": "Something went wrong"})
    with pytest.raises(ControlError, match=r"code 100001\): Something went wrong"):
        write_port_control(make_client(api), "1", 1, {"mode": "manual", "state": True, "speed": 7})


def test_writes_spaced_across_threads():
    api = FakeAPI()
    for _ in range(4):
        api.queue("/dev/getdevModeSettingList", (200, {"code": 200, "data": full_record()}))
    c = make_client(api)
    outcomes = []
    barrier = threading.Barrier(4)

    def go(speed):
        barrier.wait()
        try:
            write_port_control(c, "1", 1, {"mode": "manual", "state": True, "speed": speed})
            outcomes.append("sent")
        except control.RateLimitError:
            outcomes.append("limited")

    ts = [threading.Thread(target=go, args=(s,)) for s in (3, 4, 6, 8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert outcomes.count("sent") == 1
    assert api.count("/dev/addDevMode") == 1


def test_noop_write_not_sent_when_live_state_matches():
    api = FakeAPI()
    api.queue("/dev/getdevModeSettingList", (200, {"code": 200, "data": full_record(atType=2, onSpead=7)}))
    live = {"port": 1, "speak": 7, "loadState": 1, "curMode": 2}
    out = write_port_control(make_client(api), "1", 1, {"mode": "manual", "state": True, "speed": 7}, live_port=live)
    assert out["status"] == "no_change"
    assert api.count("/dev/addDevMode") == 0


@pytest.mark.parametrize("live", [
    {"port": 1, "speak": 5, "loadState": 1, "curMode": 2},   # HA #166: cloud says 7, device runs 5
    {"port": 1, "speak": 7, "loadState": 1},                 # no live mode reported → can't be sure
    None,                                                    # live read failed
])
def test_same_settings_resent_unless_live_confirms(live):
    api = FakeAPI()
    api.queue("/dev/getdevModeSettingList", (200, {"code": 200, "data": full_record(atType=2, onSpead=7)}))
    out = write_port_control(make_client(api), "1", 1, {"mode": "manual", "state": True, "speed": 7}, live_port=live)
    assert out["status"] == "sent"
    assert api.count("/dev/addDevMode") == 1


def test_is_noop_detects_change():
    raw = full_record(atType=2, onSpead=7, offSpead=0)
    assert is_noop(raw, {"devId": "1", "port": 1, "atType": 2, "onSpead": 7, "offSpead": 0})
    assert not is_noop(raw, {"devId": "1", "port": 1, "atType": 2, "onSpead": 8, "offSpead": 0})


# ── 3. Snapshots and restore ────────────────────────────────────────


def test_snapshot_saved_before_write_and_pruned():
    for i in range(storage.SNAPSHOTS_KEPT_PER_PORT + 3):
        control._reset_rate_limit()
        api = FakeAPI()
        api.queue("/dev/getdevModeSettingList", (200, {"code": 200, "data": full_record(onSpead=i % 10)}))
        write_port_control(make_client(api), "1", 1, {"mode": "manual", "state": True, "speed": (i + 1) % 10 or 1})
    assert storage.count_settings_snapshots("1", 1) == storage.SNAPSHOTS_KEPT_PER_PORT
    snap = storage.latest_settings_snapshot("1", 1)
    assert snap["record"]["devSetting"] == {"devId": "1", "tempUnit": 0}


def test_snapshot_table_added_to_existing_db(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, dev_id TEXT, ts INTEGER, temp_c REAL,"
                 " humidity_pct REAL, vpd_kpa REAL, fan INTEGER, sensors_json TEXT)")
    conn.execute("INSERT INTO readings (dev_id, ts, temp_c) VALUES ('1', 100, 20.0)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(storage, "DB_PATH", str(db))
    storage.init_db()
    storage.init_db()
    assert storage.count_readings("1", 0, 200) == 1
    storage.save_settings_snapshot("1", 1, {"a": 1})
    assert storage.latest_settings_snapshot("1", 1)["record"] == {"a": 1}


def test_restore_writes_snapshot_values():
    api = FakeAPI()
    api.queue("/dev/getdevModeSettingList", (200, {"code": 200, "data": full_record(atType=2, onSpead=9)}))
    snapshot = full_record(atType=1, onSpead=3, offSpead=0)
    out = write_port_control(make_client(api), "1", 1, {}, restore_record=snapshot)
    _, query, _, _ = [x for x in api.calls if x[0].endswith("/dev/addDevMode")][-1]
    assert query["atType"] == "1" and query["onSpead"] == "3"
    assert out["expected"] == {"atType": 1, "speed": 0}
    assert storage.latest_settings_snapshot("1", 1)["source"] == "restore"


# ── 4. Verification ────────────────────────────────────────────────


def _dev(speed, load, mode):
    return [{"devId": "1", "deviceInfo": {"ports": [{"port": 1, "speak": speed, "loadState": load, "curMode": mode}]}}]


class LiveClient:
    def __init__(self, frames):
        self.frames = list(frames)

    def get_devices(self):
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]


def test_verify_applied_after_lag():
    c = LiveClient([_dev(5, 1, 2), _dev(5, 1, 2), _dev(7, 1, 2)])
    vid = verify.start(c, "1", 1, {"atType": 2, "speed": 7}, fmt="query", window=60, poll=10,
                       sleep=lambda s: None, run_async=False)
    assert verify.get_result(vid)["status"] == "applied"


def test_verify_not_applied_when_device_ignores():
    c = LiveClient([_dev(5, 1, 2)])
    vid = verify.start(c, "1", 1, {"atType": 2, "speed": 7}, fmt="query", window=0, poll=10,
                       sleep=lambda s: None, run_async=False)
    r = verify.get_result(vid)
    assert r["status"] == "not_applied"
    assert "ACINFINITY_WRITE_FORMAT=form" in r["hint"]


def test_verify_condition_mode_matches_mode_not_speed():
    c = LiveClient([_dev(0, 0, 3)])  # auto mode, fan idle because conditions aren't met
    vid = verify.start(c, "1", 1, {"atType": 3}, fmt="form", window=0, poll=10, sleep=lambda s: None,
                       run_async=False)
    assert verify.get_result(vid)["status"] == "applied"


def test_verify_off():
    c = LiveClient([_dev(0, 0, 1)])
    vid = verify.start(c, "1", 1, {"atType": 1, "speed": 0}, fmt="query", window=0, poll=10,
                       sleep=lambda s: None, run_async=False)
    assert verify.get_result(vid)["status"] == "applied"


def test_verify_ambiguous_off_is_unconfirmed_without_mode():
    # Port was idling at 0 under Auto; Off requested; live data has no curMode → can't tell.
    base = {"port": 1, "speak": 0, "loadState": 0}
    c = LiveClient([[{"devId": "1", "deviceInfo": {"ports": [base]}}]])
    vid = verify.start(c, "1", 1, {"atType": 1, "speed": 0}, fmt="query", baseline=base, window=0,
                       sleep=lambda s: None, run_async=False)
    assert verify.get_result(vid)["status"] == "unconfirmed"


def test_verify_ambiguous_off_applied_when_mode_changes():
    base = {"port": 1, "speak": 0, "loadState": 0, "curMode": 3}
    c = LiveClient([_dev(0, 0, 3), _dev(0, 0, 1)])
    vid = verify.start(c, "1", 1, {"atType": 1, "speed": 0}, fmt="query", baseline=base, window=60, poll=10,
                       sleep=lambda s: None, run_async=False)
    assert verify.get_result(vid)["status"] == "applied"


def test_verify_ambiguous_ignored_is_not_applied():
    base = {"port": 1, "speak": 0, "loadState": 0, "curMode": 3}
    c = LiveClient([_dev(0, 0, 3)])  # stays in Auto: the Off write was ignored
    vid = verify.start(c, "1", 1, {"atType": 1, "speed": 0}, fmt="query", baseline=base, window=0,
                       sleep=lambda s: None, run_async=False)
    assert verify.get_result(vid)["status"] == "not_applied"


def test_verify_waits_one_poll_before_judging_ambiguous():
    sleeps = []
    base = {"port": 1, "speak": 7, "loadState": 1, "curMode": 2}
    c = LiveClient([_dev(7, 1, 2)])
    verify.start(c, "1", 1, {"atType": 2, "speed": 7}, fmt="query", baseline=base, window=60, poll=10,
                 sleep=sleeps.append, run_async=False)
    assert sleeps and sleeps[0] == 10


def test_verify_results_bounded():
    c = LiveClient([_dev(0, 0, 1)])
    ids = [verify.start(c, "1", 1, {"atType": 1}, fmt="q", window=0, sleep=lambda s: None, run_async=False)
           for _ in range(verify._MAX_RESULTS + 5)]
    assert verify.get_result(ids[0]) is None
    assert verify.get_result(ids[-1]) is not None


def test_expected_state():
    assert expected_state({"atType": 2, "onSpead": 6}) == {"atType": 2, "speed": 6}
    assert expected_state({"atType": 1}) == {"atType": 1, "speed": 0}
    assert expected_state({"atType": 8, "onSpead": 6}) == {"atType": 8}


# ── 5. Signing ─────────────────────────────────────────────────────


def test_sign_headers_recipe():
    import hashlib

    def md5(t):
        return hashlib.md5(t.encode()).hexdigest()

    h = client_mod.sign_headers("tok", "sec", "app", request_id="1700000000000")
    assert h["sign"] == md5(md5("tok2.0.8") + md5("secapp1700000000000"))
    assert h["requestApp"] == "app" and h["requestId"] == "1700000000000" and h["version"] == "2.0.8"


def test_no_sign_headers_by_default(monkeypatch):
    _, _, _, headers = _write_via_api(monkeypatch, None)
    assert not {"sign", "requestapp", "requestid"} & set(headers)


def test_sign_headers_when_enabled(monkeypatch):
    monkeypatch.setenv("ACINFINITY_SIGN_WRITES", "1")
    _, _, _, headers = _write_via_api(monkeypatch, None)
    assert headers["sign"] and headers["requestapp"] == "r"


# ── routes ─────────────────────────────────────────────────────────


@pytest.fixture
def routes(monkeypatch):
    monkeypatch.setenv("ACDASH_USE_ENV_CREDENTIALS", "1")
    monkeypatch.setenv("ACINFINITY_EMAIL", "user@example.com")
    monkeypatch.setenv("ACINFINITY_PASSWORD", "right")
    import app.main as main
    import app.session as session
    from fastapi.testclient import TestClient

    api = FakeAPI()
    monkeypatch.setattr(session, "ACInfinityClient", lambda e, p: make_client(api, password=p, email=e))
    started = []
    monkeypatch.setattr(main.verify, "start", lambda *a, **k: started.append((a, k)) or "vid123")
    session.reset_client()
    main._clear_cache()
    yield TestClient(main.app), api, started
    session.reset_client()


def test_port_control_route_returns_pending_with_ok(routes):
    tc, api, started = routes
    api.queue("/dev/getdevModeSettingList", (200, {"code": 200, "data": full_record()}))
    r = tc.post("/api/port-control", json={"dev_id": "1", "port": 1, "mode": "manual", "state": True, "speed": 7})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["status"] == "pending" and body["verify_id"] == "vid123" and body["format"] == "query"
    assert started[0][0][3] == {"atType": 2, "speed": 7}


def test_port_control_route_no_change(routes):
    tc, api, started = routes
    api.queue("/user/devInfoListAll", (200, {"code": 200, "data": [{"devId": "1", "deviceInfo": {"ports": [
        {"port": 1, "speak": 7, "loadState": 1, "curMode": 2}]}}]}))
    api.queue("/dev/getdevModeSettingList", (200, {"code": 200, "data": full_record(atType=2, onSpead=7)}))
    r = tc.post("/api/port-control", json={"dev_id": "1", "port": 1, "mode": "manual", "state": True, "speed": 7})
    assert r.json() == {"ok": True, "status": "no_change", "format": "query"}
    assert started == []


def test_verify_route(routes, monkeypatch):
    tc, _api, _ = routes
    verify._store("abc", {"status": "applied"})
    assert tc.get("/api/port-control/verify?id=abc").json()["status"] == "applied"
    assert tc.get("/api/port-control/verify?id=nope").status_code == 404


def test_restore_route(routes):
    tc, api, started = routes
    r = tc.post("/api/port-restore", json={"dev_id": "1", "port": 1})
    assert r.status_code == 404
    storage.save_settings_snapshot("1", 1, full_record(atType=1, onSpead=2))
    api.queue("/dev/getdevModeSettingList", (200, {"code": 200, "data": full_record(atType=2, onSpead=9)}))
    r = tc.post("/api/port-restore", json={"dev_id": "1", "port": 1})
    assert r.json()["status"] == "pending"
    _, query, _, _ = [x for x in api.calls if x[0].endswith("/dev/addDevMode")][-1]
    assert query["atType"] == "1"


def test_port_settings_reports_restore_availability(routes):
    tc, api, _ = routes
    api.queue("/dev/getdevModeSettingList", (200, {"code": 200, "data": full_record()}),
              (200, {"code": 200, "data": full_record()}))
    assert tc.get("/api/port-settings?dev_id=1&port=1").json()["restore_available"] is False
    storage.save_settings_snapshot("1", 1, full_record())
    body = tc.get("/api/port-settings?dev_id=1&port=1").json()
    assert body["restore_available"] is True and body["restore_ts"] and body["write_format"] == "query"
