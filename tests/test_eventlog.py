"""add-event-log-view: API call, decoding, paging, route (filter, cache, read-only)."""
from __future__ import annotations

import pytest

from app import eventlog
from app.eventlog import describe, fetch_events
from tests.fakes import FakeAPI, make_client

NOW = 1_800_000_000


# ── 1. API call ────────────────────────────────────────────────────


def test_event_log_page_params_and_cursor():
    api = FakeAPI()
    api.queue("/log/logdataByAll", (200, {"code": 200, "data": {"rows": [{"id": 5}], "total": 1}}))
    c = make_client(api)
    data = c.event_log_page("9", NOW, NOW - 86400, cursor=123, page_size=50)
    assert data["rows"] == [{"id": 5}]
    path, query, form, _ = [x for x in api.calls if x[0].endswith("/log/logdataByAll")][-1]
    assert query == {"appId": "tok-1", "devId": "9", "id": "123", "time": str(NOW), "endTime": str(NOW - 86400),
                     "pageSize": "50", "orderDirection": "1"}
    assert form == {}


def test_event_log_page_failure_returns_none():
    api = FakeAPI()
    api.queue("/log/logdataByAll", *[(200, {"code": 999998, "msg": "Rate Limiting!"})] * 2)
    c = make_client(api)
    assert c.event_log_page("9", NOW, NOW - 60) is None
    assert "rate limiting" in c.last_request_error.lower()


# ── decoding (one per table) ───────────────────────────────────────


@pytest.mark.parametrize("raw,text", [
    ({"logType": 3, "businessType": 1, "portSelection": 2, "mlVariationTrend": 2, "pauseReason": 4},
     "AI increased port 2 to lower humidity"),
    ({"logType": 3, "businessType": 1, "portSelection": 1, "mlVariationTrend": 1, "pauseReason": 6, "mlVariation": 2},
     "AI decreased port 1 (level 2) to lower VPD"),
    ({"logType": 3, "businessType": 2, "mlVariationType": 1}, "AI target range updated"),
    ({"logType": 3, "businessType": 2, "mlVariationType": 99}, "AI setting changed (type 99)"),
    ({"logType": 3, "businessType": 3, "mlVariationType": 0}, "AI paused"),
    ({"logType": 3, "businessType": 3, "mlVariationType": 16}, "Night mode on"),
    ({"logType": 2, "businessType": 1, "isActivateAlarmHightemp": 1}, "High temperature alarm triggered"),
    ({"logType": 2, "businessType": 1, "isDeactivateAlarmLowYemp": 1}, "Low temperature alarm cleared"),
    ({"logType": 2, "businessType": 0}, "Alarm"),
    ({"logType": 4, "businessType": 0, "portSelection": 1, "currentMode": 2, "fanSpeedOn": 5}, "Port 1 mode: On (speed 5)"),
    ({"logType": 4, "portSelection": 3, "currentMode": 6, "cycleOn": 15, "cycleOff": 45},
     "Port 3 mode: Cycle (15 min on / 45 min off)"),
    ({"logType": 4, "portSelection": 2, "advanceName": "Night"}, "Automation “Night” ran on port 2"),
    ({"logType": 5, "businessType": 2}, "Low water detected"),
    ({"logType": 5, "businessType": 42}, "Controller notice (type 42)"),
    ({"logType": 9, "businessType": 7}, "Unrecognized event (type 9/7)"),
])
def test_describe(raw, text):
    assert describe(raw)["text"] == text


def test_describe_time_and_port():
    ev = describe({"logType": 4, "logTime": NOW, "portSelection": 0, "portIndex": 3, "currentMode": 1})
    assert ev["time"] == NOW and ev["port"] == 3
    assert describe({"logType": 4, "createTime": NOW * 1000})["time"] == NOW  # ms fallback


# ── paging ─────────────────────────────────────────────────────────


class PagedClient:
    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda r: r["id"], reverse=True)
        self.calls = []
        self.last_request_error = None

    def event_log_page(self, dev_id, newer, older, *, cursor=0, page_size=200):
        self.calls.append(cursor)
        rows = [r for r in self.rows if not cursor or r["id"] < cursor]
        return {"rows": rows[:page_size]}


def _rows(n, start=NOW - 10):
    return [{"id": i + 1, "logTime": start - (n - i) * 60, "logType": 4, "currentMode": 1} for i in range(n)]


def test_fetch_pages_by_cursor_and_stops_at_limit():
    c = PagedClient(_rows(450))
    out = fetch_events(c, "1", now=NOW, hours=24, limit=200, page_size=100)
    assert len(out["events"]) == 200 and out["truncated"]
    assert c.calls[:2] == [0, 351]


def test_fetch_drops_events_outside_window():
    rows = _rows(3) + [{"id": 0, "logTime": NOW - 5 * 86400, "logType": 4}]
    out = fetch_events(PagedClient(rows), "1", now=NOW, hours=24)
    assert len(out["events"]) == 3


def test_fetch_error_reported():
    class Down:
        last_request_error = "AC Infinity is rate limiting — try again shortly"

        def event_log_page(self, *a, **k):
            return None

    out = fetch_events(Down(), "1", now=NOW)
    assert out["events"] == [] and "rate limiting" in out["error"]


# ── 2. Route ───────────────────────────────────────────────────────


@pytest.fixture
def route(monkeypatch):
    monkeypatch.setenv("ACDASH_USE_ENV_CREDENTIALS", "1")
    monkeypatch.setenv("ACINFINITY_EMAIL", "user@example.com")
    monkeypatch.setenv("ACINFINITY_PASSWORD", "right")
    import app.main as main
    import app.session as session
    from fastapi.testclient import TestClient

    api = FakeAPI()
    api.queue("/user/devInfoListAll", *[(200, {"code": 200, "data": [{"devId": "1", "devName": "T",
                                                                        "deviceInfo": {"ports": []}}]})] * 20)
    monkeypatch.setattr(session, "ACInfinityClient", lambda e, p: make_client(api, password=p, email=e))
    session.reset_client()
    main._clear_cache()
    main._activity_cache.clear()
    yield TestClient(main.app), api
    session.reset_client()
    main._activity_cache.clear()


def _log_rows():
    import time
    now = int(time.time())
    return {"code": 200, "data": {"rows": [
        {"id": 3, "logTime": now - 60, "logType": 4, "portSelection": 1, "currentMode": 2, "fanSpeedOn": 4},
        {"id": 2, "logTime": now - 120, "logType": 4, "portSelection": 2, "currentMode": 1},
        {"id": 1, "logTime": now - 180, "logType": 5, "businessType": 2},
    ]}}


def test_activity_route_lists_newest_first(route):
    tc, api = route
    api.queue("/log/logdataByAll", (200, _log_rows()))
    body = tc.get("/api/activity?dev_id=1").json()
    assert [e["text"] for e in body["events"]] == ["Port 1 mode: On (speed 4)", "Port 2 mode: Off", "Low water detected"]


def test_activity_route_filters_port_and_caches(route):
    tc, api = route
    api.queue("/log/logdataByAll", (200, _log_rows()))
    tc.get("/api/activity?dev_id=1")
    body = tc.get("/api/activity?dev_id=1&port=2").json()
    assert [e["port"] for e in body["events"]] == [2]
    assert api.count("/log/logdataByAll") == 1  # second call served from the 60 s cache


def test_activity_route_unknown_controller(route):
    tc, _api = route
    assert tc.get("/api/activity?dev_id=nope").status_code == 404


def test_activity_route_is_read_only(route):
    tc, api = route
    api.queue("/log/logdataByAll", (200, _log_rows()))
    tc.get("/api/activity?dev_id=1")
    paths = {p for p, *_ in api.calls}
    assert all(("log/log" not in p or p.endswith("logdataByAll")) for p in paths)
    assert not any(p.endswith(("/addDevMode", "/delByid")) for p in paths)
