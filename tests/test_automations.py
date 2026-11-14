"""Automation create / toggle / delete — must match app 2.0.0 (verified live on a real controller, 2026-11-14)."""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import control
from app.control import (
    AUTOMATION_CONTINUOUS,
    ControlError,
    UpstreamError,
    build_automation_payload,
    create_automation,
    day_byte,
    delete_automation,
    get_automations,
    normalize_automations,
    set_automation_enabled,
)
from tests.fakes import FakeAPI, make_client


@pytest.fixture
def api():
    return FakeAPI()


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    """Fake time for the confirm polls: sleep advances the clock, nothing really waits."""
    now = [0.0]
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    monkeypatch.setattr(control, "_sleep", sleep)
    monkeypatch.setattr(control, "_clock", lambda: now[0])
    return slept


def _last(api, suffix):
    return [c for c in api.calls if c[0].endswith(suffix)][-1]


def _groups(*items):
    return (200, {"code": 200, "msg": "success", "data": list(items)})


# ── raw client calls ───────────────────────────────────────────────


def test_delete_sends_app_params_as_query(api):
    c = make_client(api)
    c.delete_automation_raw("9", "77")
    _, query, form, _ = _last(api, "/delByid")
    assert query == {"advId": "77", "isDel": "true", "isflag": "true"}
    assert form == {}  # the old form body (no isDel) ENABLED the automation instead of deleting


def test_toggle_sends_app_params_as_query(api):
    c = make_client(api)
    c.toggle_automation_raw("9", "77")
    _, query, form, _ = _last(api, "/updateGroupsIsOn")
    assert query == {"advId": "77", "isDel": "0", "isflag": "1"}
    assert form == {}


def test_create_sends_full_record_as_query(api):
    c = make_client(api)
    payload = build_automation_payload(name="Night", ports=[2], begin_mins=1320, end_mins=360, on_speed=3)
    c.create_automation_raw("9", payload)
    _, query, form, _ = _last(api, "/addGroups")
    assert form == {}
    assert query["devId"] == "9" and query["isFlag"] == "true" and query["returnData"] == "true"
    assert query["switchTime"] == "127" and query["currentMode"] == "1"
    assert query["beginTime"] == "1320" and query["endTime"] == "360"
    assert query["grouptDevType"] == "2" and query["onSpeed"] == "3"
    assert len(query) > 70  # the whole NetAutomation record, like the app


@pytest.mark.parametrize("response", [
    (500, {}),
    (200, {"code": 999999, "msg": "Operation failed"}),
    (200, {"code": 200, "data": {"not": "a list"}}),
])
def test_failed_group_read_is_none_not_empty(api, response):
    c = make_client(api)
    api.queue("/getGroups", response)
    assert c.get_automations_raw("9") is None


def test_group_read_drops_non_dict_entries(api):
    c = make_client(api)
    api.queue("/getGroups", _groups({"advId": 1}, "junk", None, 5))
    assert c.get_automations_raw("9") == [{"advId": 1}]


def test_dev_version_cached_from_device_list(api):
    c = make_client(api)
    api.queue("/user/devInfoListAll", (200, {"code": 200, "data": [{"devId": "9", "devType": 11, "devVersion": 3}]}))
    c.get_devices()
    assert c.dev_version("9") == 3 and c.dev_version("other") == 0


# ── payload building ───────────────────────────────────────────────


def test_day_byte():
    assert day_byte(None) == 127
    assert day_byte(["mon", "Fri"]) == 1 | 16
    assert day_byte(["sunday"]) == 64
    assert day_byte(["mon"], continuous=True) == AUTOMATION_CONTINUOUS | 127


@pytest.mark.parametrize("bad", [["funday"], ["monkey"], ["mo"], [1], ["satur"]])
def test_day_byte_rejects_non_day_names(bad):
    with pytest.raises(ValueError):
        day_byte(bad)


def test_payload_never_continuous_by_default():
    p = build_automation_payload(name="x", ports=[1], begin_mins=60, end_mins=120)
    assert not p["switchTime"] & AUTOMATION_CONTINUOUS  # bit 7 would ignore the window (ran 24/7 live)


def test_payload_port_bitmask_and_off_mode():
    p = build_automation_payload(name="x", ports=[1, 3, 3], begin_mins=0, end_mins=60, mode=2, off_speed=1)
    assert p["grouptDevType"] == 0b101 and p["currentMode"] == 2 and p["offSpeed"] == 1


@pytest.mark.parametrize("kwargs", [
    {"name": "", "ports": [1], "begin_mins": 0, "end_mins": 60},
    {"name": "   ", "ports": [1], "begin_mins": 0, "end_mins": 60},
    {"name": "x", "ports": [], "begin_mins": 0, "end_mins": 60},
    {"name": "x", "ports": [9], "begin_mins": 0, "end_mins": 60},
    {"name": "x", "ports": [1], "begin_mins": 0, "end_mins": 1440},
    {"name": "x", "ports": [1], "begin_mins": 60, "end_mins": 60},
    {"name": "x", "ports": [1], "begin_mins": 0, "end_mins": 60, "on_speed": 11},
    {"name": "x", "ports": [1], "begin_mins": 0, "end_mins": 60, "mode": 4},
])
def test_payload_rejects_bad_input(kwargs):
    with pytest.raises(ValueError):
        build_automation_payload(**kwargs)


# ── stateful fake client (server FLIPS state on toggle) ────────────


class FakeAutoClient:
    def __init__(self, recs=None, *, flips=True, lag_reads=0, fail_reads=None):
        self.recs = {r["advId"]: dict(r) for r in (recs if recs is not None else
                                                   [{"advId": "5", "advName": "Night", "isOn": 0}])}
        self.flips = flips
        self.lag_reads = lag_reads        # reads after a write that still show the old state
        self.fail_reads = set(fail_reads or ())  # read numbers (1-based) that fail
        self.reads = 0
        self.toggles = 0
        self.deletes = 0
        self.creates = 0
        self._pending = None

    def get_automations_raw(self, dev_id):
        self.reads += 1
        if self.reads in self.fail_reads:
            return None
        if self._pending is not None:
            if self.lag_reads > 0:
                self.lag_reads -= 1
            else:
                self._pending()
                self._pending = None
        return [dict(r) for r in self.recs.values()]

    def toggle_automation_raw(self, dev_id, adv_id):
        self.toggles += 1
        if self.flips:
            def apply():
                self.recs[adv_id]["isOn"] = 0 if self.recs[adv_id]["isOn"] else 1
            self._pending = apply
        return {"code": 200}

    def delete_automation_raw(self, dev_id, adv_id):
        self.deletes += 1
        if self.flips:
            self._pending = lambda: self.recs.pop(adv_id, None)
        return {"code": 200}

    def create_automation_raw(self, dev_id, payload):
        self.creates += 1
        if self.flips:
            new = {"advId": str(100 + self.creates), "advName": payload["advName"], "isOn": payload["isOn"]}
            self._pending = lambda: self.recs.__setitem__(new["advId"], new)
        return {"code": 200}


def test_enable_flips_once_when_off():
    c = FakeAutoClient()
    assert set_automation_enabled(c, "9", "5", True) == "changed"
    assert c.toggles == 1 and c.recs["5"]["isOn"] == 1


def test_enable_waits_for_lagging_read(fast_clock):
    c = FakeAutoClient(lag_reads=2)
    assert set_automation_enabled(c, "9", "5", True) == "changed"
    assert c.toggles == 1 and len(fast_clock) == 2  # slept, re-read, then saw the change


def test_enable_when_already_on_does_not_flip():
    c = FakeAutoClient([{"advId": "5", "advName": "Night", "isOn": 1}])
    assert set_automation_enabled(c, "9", "5", True) == "unchanged"
    assert c.toggles == 0


def test_enable_reports_when_state_never_changes(fast_clock):
    c = FakeAutoClient(flips=False)
    with pytest.raises(UpstreamError):
        set_automation_enabled(c, "9", "5", True)
    assert c.toggles == 1
    assert sum(fast_clock) <= control.AUTOMATION_CONFIRM_SECS + control._POLL_SECS  # bounded wait


def test_enable_missing_automation():
    c = FakeAutoClient([])
    with pytest.raises(ControlError) as e:
        set_automation_enabled(c, "9", "5", True)
    assert not isinstance(e.value, UpstreamError)


def test_enable_refuses_when_first_read_fails():
    c = FakeAutoClient(fail_reads={1})
    with pytest.raises(UpstreamError):
        set_automation_enabled(c, "9", "5", True)
    assert c.toggles == 0  # never flip blind


def test_enable_rejected_by_server():
    class Rejecting(FakeAutoClient):
        def toggle_automation_raw(self, dev_id, adv_id):
            return {"code": 999999, "msg": "Operation failed"}

    with pytest.raises(ControlError, match="Operation failed"):
        set_automation_enabled(Rejecting(), "9", "5", True)


def test_enable_unreachable_is_upstream():
    class Down(FakeAutoClient):
        def toggle_automation_raw(self, dev_id, adv_id):
            return None

    with pytest.raises(UpstreamError):
        set_automation_enabled(Down(), "9", "5", True)


def test_double_click_does_not_cancel_out():
    """Two overlapping 'enable' requests: one flips, the other sees it already on."""
    c = FakeAutoClient()
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def go():
        barrier.wait()
        outcomes.append(set_automation_enabled(c, "9", "5", True))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["changed", "unchanged"]
    assert c.toggles == 1 and c.recs["5"]["isOn"] == 1


def test_delete_confirms_gone():
    c = FakeAutoClient()
    assert delete_automation(c, "9", "5") == "deleted"
    assert c.deletes == 1 and "5" not in c.recs


def test_delete_reports_when_still_listed():
    c = FakeAutoClient(flips=False)
    with pytest.raises(UpstreamError):
        delete_automation(c, "9", "5")


def test_delete_already_gone_is_noop():
    c = FakeAutoClient([])
    assert delete_automation(c, "9", "5") == "already_gone"
    assert c.deletes == 0


def test_delete_failed_first_read_is_error_not_gone():
    c = FakeAutoClient(fail_reads={1})
    with pytest.raises(UpstreamError):
        delete_automation(c, "9", "5")
    assert c.deletes == 0


def test_delete_never_confirms_gone_from_failed_reads():
    """Every read after the delete fails: must NOT report success."""
    c = FakeAutoClient(flips=False, fail_reads=set(range(2, 100)))
    with pytest.raises(UpstreamError):
        delete_automation(c, "9", "5")


def test_delete_survives_one_failed_confirm_read():
    c = FakeAutoClient(fail_reads={2})
    assert delete_automation(c, "9", "5") == "deleted"


def _payload(name="Day", **kw):
    return build_automation_payload(name=name, ports=[2], begin_mins=360, end_mins=1320, **kw)


def test_create_confirms_listed_and_returns_id():
    c = FakeAutoClient()
    assert create_automation(c, "9", _payload()) == "101"
    assert c.creates == 1


def test_create_rejects_duplicate_name():
    c = FakeAutoClient()
    with pytest.raises(ControlError, match="already exists"):
        create_automation(c, "9", _payload(name="  night "))
    assert c.creates == 0


def test_double_submitted_create_makes_one():
    c = FakeAutoClient()
    create_automation(c, "9", _payload())
    with pytest.raises(ControlError, match="already exists"):
        create_automation(c, "9", _payload())
    assert c.creates == 1


def test_create_refuses_when_list_unreadable():
    c = FakeAutoClient(fail_reads={1})
    with pytest.raises(UpstreamError):
        create_automation(c, "9", _payload())
    assert c.creates == 0


def test_create_reports_when_never_listed():
    c = FakeAutoClient(flips=False)
    with pytest.raises(UpstreamError, match="isn't listed"):
        create_automation(c, "9", _payload())


def test_get_automations_raises_on_failed_read():
    with pytest.raises(UpstreamError):
        get_automations(FakeAutoClient(fail_reads={1}), "9")


# ── normalize ──────────────────────────────────────────────────────


def test_normalize_separates_enabled_from_running():
    out = normalize_automations([{"advId": 1, "advName": "N", "isOn": 0, "runState": 1, "grouptDevType": 2,
                                  "currentMode": 1, "switchTime": 1 | 64, "beginTime": 1320, "endTime": 360}])
    a = out[0]
    assert a["is_on"] is False and a["running"] is True
    assert a["ports"] == [2] and a["mode"] == "on" and a["days"] == ["mon", "sun"]
    assert a["continuous"] is False and (a["begin_mins"], a["end_mins"]) == (1320, 360)


def test_normalize_keeps_same_named_automations_apart():
    out = normalize_automations([{"advId": 1, "advName": "N"}, {"advId": 2, "advName": "N"}, {"advId": 1, "advName": "N"}])
    assert [a["adv_id"] for a in out] == ["1", "2"]


def test_normalize_tolerates_junk():
    out = normalize_automations(["x", None, {"advName": "no id"}, {"advId": 3, "isOn": "1", "grouptDevType": "bad"}])
    assert len(out) == 1 and out[0]["is_on"] is True and out[0]["ports"] == []


# ── endpoints ──────────────────────────────────────────────────────


@pytest.fixture
def app_env(monkeypatch, api):
    monkeypatch.setenv("ACDASH_USE_ENV_CREDENTIALS", "1")
    monkeypatch.setenv("ACINFINITY_EMAIL", "user@example.com")
    monkeypatch.setenv("ACINFINITY_PASSWORD", "right")
    import app.main as main
    import app.session as session

    monkeypatch.setattr(session, "ACInfinityClient", lambda e, p: make_client(api, password=p, email=e))
    session.reset_client()
    main._clear_cache()
    control._reset_rate_limit()
    yield TestClient(main.app, base_url="http://dash.local:8080")
    session.reset_client()


NIGHT = {"advId": 77, "advName": "Night", "isOn": 0, "grouptDevType": 2, "currentMode": 1,
         "switchTime": 127, "beginTime": 1320, "endTime": 360, "onSpeed": 4}


def test_create_endpoint_builds_windowed_automation(app_env, api):
    api.queue("/getGroups", _groups(), _groups({**NIGHT, "advId": 88}))
    r = app_env.post("/api/automation-create", json={"dev_id": "9", "name": "Night", "ports": [2],
                                                    "begin_mins": 1320, "end_mins": 360, "on_speed": 4,
                                                    "days": ["mon", "tue"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "adv_id": "88"}
    _, query, _, _ = _last(api, "/addGroups")
    assert query["switchTime"] == "3" and query["currentMode"] == "1" and query["onSpeed"] == "4"


def test_create_endpoint_sends_real_dev_version(app_env, api):
    api.queue("/user/devInfoListAll", (200, {"code": 200, "data": [{"devId": "9", "devType": 11, "devVersion": 3}]}))
    import app.session as session
    session.get_client("user@example.com", "right").get_devices()
    api.queue("/getGroups", _groups(), _groups({**NIGHT, "advId": 88}))
    r = app_env.post("/api/automation-create", json={"dev_id": "9", "name": "Night", "ports": [2],
                                                    "begin_mins": 1320, "end_mins": 360})
    assert r.status_code == 200, r.text
    assert _last(api, "/addGroups")[1]["devVersion"] == "3"


@pytest.mark.parametrize("body", [
    {"name": "N", "ports": [1], "begin_mins": 60, "end_mins": 60},             # same start/end
    {"name": "N", "ports": [1], "end_mins": 60},                               # missing start
    {"name": "N", "ports": [1], "begin_mins": 60},                             # missing end
    {"name": "N", "ports": "12", "begin_mins": 0, "end_mins": 60},             # ports not a list
    {"name": "N", "ports": [True], "begin_mins": 0, "end_mins": 60},           # bool port
    {"name": "N", "ports": [1], "begin_mins": 0, "end_mins": 60, "days": "sat"},
    {"name": "N", "ports": [1], "begin_mins": 0, "end_mins": 60, "days": ["monkey"]},
    {"name": "N", "ports": [1], "begin_mins": 0, "end_mins": 60, "continuous": "false"},
    {"name": "N", "ports": [1], "begin_mins": 0, "end_mins": 60, "enabled": "false"},
    {"name": "N", "ports": [1], "begin_mins": 1.5, "end_mins": 60},
    {"name": "N", "ports": [1], "begin_mins": "60", "end_mins": 120},
    {"name": "N", "ports": [1], "begin_mins": 10**30, "end_mins": 60},
    {"name": "N", "ports": [1], "begin_mins": 0, "end_mins": 60, "mode": "cycle"},
    {"name": 5, "ports": [1], "begin_mins": 0, "end_mins": 60},
])
def test_create_endpoint_rejects_bad_input(app_env, api, body):
    r = app_env.post("/api/automation-create", json={"dev_id": "9", **body})
    assert r.status_code == 400, r.text
    assert "error" in r.json()
    assert api.count("/addGroups") == 0


def test_create_endpoint_rejects_infinity(app_env, api):
    r = app_env.post("/api/automation-create", content=b'{"dev_id": "9", "name": "N", "ports": [1], '
                     b'"begin_mins": Infinity, "end_mins": 60}', headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert api.count("/addGroups") == 0


def test_create_endpoint_continuous_needs_no_times(app_env, api):
    api.queue("/getGroups", _groups(), _groups({**NIGHT, "advId": 88, "advName": "All"}))
    r = app_env.post("/api/automation-create", json={"dev_id": "9", "name": "All", "ports": [1], "continuous": True})
    assert r.status_code == 200, r.text
    assert int(_last(api, "/addGroups")[1]["switchTime"]) & AUTOMATION_CONTINUOUS


def test_create_endpoint_duplicate_name_400(app_env, api):
    api.queue("/getGroups", _groups(NIGHT))
    r = app_env.post("/api/automation-create", json={"dev_id": "9", "name": "night", "ports": [2],
                                                    "begin_mins": 0, "end_mins": 60})
    assert r.status_code == 400 and "already exists" in r.json()["error"]
    assert api.count("/addGroups") == 0


def test_list_endpoint_502_when_read_fails(app_env, api):
    api.queue("/getGroups", (500, {}), (500, {}))
    r = app_env.get("/api/automations?dev_id=9")
    assert r.status_code == 502 and "error" in r.json()


def test_list_endpoint_shows_mode_and_days(app_env, api):
    api.queue("/getGroups", _groups(NIGHT))
    a = app_env.get("/api/automations?dev_id=9").json()[0]
    assert a["adv_id"] == "77" and a["mode"] == "on" and len(a["days"]) == 7 and a["on_speed"] == 4


def test_toggle_endpoint_enables(app_env, api):
    api.queue("/getGroups", _groups(NIGHT), _groups({**NIGHT, "isOn": 1}))
    r = app_env.post("/api/automation-toggle", json={"dev_id": "9", "adv_id": "77", "is_on": True})
    assert r.status_code == 200 and r.json() == {"ok": True, "outcome": "changed"}
    assert api.count("/updateGroupsIsOn") == 1


def test_toggle_endpoint_unchanged_does_not_flip(app_env, api):
    api.queue("/getGroups", _groups({**NIGHT, "isOn": 1}))
    r = app_env.post("/api/automation-toggle", json={"dev_id": "9", "adv_id": "77", "is_on": True})
    assert r.json() == {"ok": True, "outcome": "unchanged"}
    assert api.count("/updateGroupsIsOn") == 0


@pytest.mark.parametrize("is_on", ["false", "true", 0, 1, None])
def test_toggle_endpoint_requires_real_bool(app_env, api, is_on):
    r = app_env.post("/api/automation-toggle", json={"dev_id": "9", "adv_id": "77", "is_on": is_on})
    assert r.status_code == 400
    assert api.count("/updateGroupsIsOn") == 0


def test_toggle_endpoint_502_when_read_fails(app_env, api):
    api.queue("/getGroups", (500, {}), (500, {}))
    r = app_env.post("/api/automation-toggle", json={"dev_id": "9", "adv_id": "77", "is_on": True})
    assert r.status_code == 502
    assert api.count("/updateGroupsIsOn") == 0


def test_toggle_endpoint_missing_is_400(app_env, api):
    api.queue("/getGroups", _groups())
    r = app_env.post("/api/automation-toggle", json={"dev_id": "9", "adv_id": "77", "is_on": True})
    assert r.status_code == 400


def test_delete_endpoint_confirms(app_env, api):
    api.queue("/getGroups", _groups(NIGHT), _groups())
    r = app_env.request("DELETE", "/api/automation", json={"dev_id": "9", "adv_id": "77"})
    assert r.status_code == 200 and r.json() == {"ok": True, "outcome": "deleted"}
    assert api.count("/delByid") == 1


def test_delete_endpoint_502_when_read_fails(app_env, api):
    api.queue("/getGroups", *[(500, {})] * 2)
    r = app_env.request("DELETE", "/api/automation", json={"dev_id": "9", "adv_id": "77"})
    assert r.status_code == 502
    assert api.count("/delByid") == 0


def test_delete_endpoint_502_when_confirm_reads_fail(app_env, api):
    api.queue("/getGroups", _groups(NIGHT), *[(500, {})] * 40)
    r = app_env.request("DELETE", "/api/automation", json={"dev_id": "9", "adv_id": "77"})
    assert r.status_code == 502  # never "deleted" on the strength of failed reads
    assert api.count("/delByid") == 1


@pytest.mark.parametrize("body", [{"dev_id": "9"}, {"adv_id": "77"}, {"dev_id": True, "adv_id": "77"},
                                  {"dev_id": "9", "adv_id": ["77"]}])
def test_delete_endpoint_bad_ids(app_env, api, body):
    r = app_env.request("DELETE", "/api/automation", json=body)
    assert r.status_code == 400
