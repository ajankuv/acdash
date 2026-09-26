"""service-status: /status freshness; /health unchanged."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import status as st


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("STATUS_STALE_SECONDS", raising=False)
    st._reset(started=1000.0)
    yield
    st._reset()


def test_starting_before_first_success():
    code, body = st.snapshot(60, now=1030.0)
    assert (code, body["state"]) == (200, "starting")


def test_stale_when_never_succeeded_after_grace():
    st.record_error("Could not sign in to AC Infinity: Incorrect Password", now=1010.0)
    code, body = st.snapshot(60, now=1000.0 + 181)
    assert (code, body["state"]) == (503, "stale")
    assert "Incorrect Password" in body["last_error"]


def test_ok_when_recent():
    st.record_success(now=2000.0)
    code, body = st.snapshot(60, now=2040.0)
    assert (code, body["state"], body["seconds_since_success"]) == (200, "ok", 40.0)
    assert body["last_error"] is None


def test_stale_after_ten_minutes():
    st.record_success(now=2000.0)
    st.record_error("Could not reach AC Infinity (ConnectError).", now=2300.0)
    code, body = st.snapshot(60, now=2600.0)
    assert (code, body["state"]) == (503, "stale")
    assert "ConnectError" in body["last_error"]


def test_old_error_hidden_after_recovery():
    st.record_error("boom", now=1900.0)
    st.record_success(now=2000.0)
    assert st.snapshot(60, now=2010.0)[1]["last_error"] is None


@pytest.mark.parametrize("raw,expected", [(None, 180.0), ("90", 90.0), ("x", 180.0), ("0", 1.0)])
def test_threshold(monkeypatch, raw, expected):
    if raw is not None:
        monkeypatch.setenv("STATUS_STALE_SECONDS", raw)
    assert st.stale_after_seconds(60) == expected


def test_route_and_health(monkeypatch):
    monkeypatch.setenv("ACDASH_USE_ENV_CREDENTIALS", "1")
    monkeypatch.setenv("ACINFINITY_EMAIL", "user@example.com")
    monkeypatch.setenv("ACINFINITY_PASSWORD", "hunter-not-real")
    import app.main as main
    tc = TestClient(main.app)
    st._reset()
    st.record_success()
    r = tc.get("/status")
    assert r.status_code == 200 and r.json()["state"] == "ok"
    assert {"enabled", "running"} <= set(r.json()["backfill"])
    text = json.dumps(r.json())
    for secret in ("hunter-not-real", "user@example.com", "token"):
        assert secret not in text
    st._reset(started=0.0)  # long uptime, never succeeded → stale
    assert tc.get("/status").status_code == 503
    assert tc.get("/health").status_code == 200 and tc.get("/health").text == "OK"


def test_fetch_records_success_and_error(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "_fetch_controllers", lambda: ([{"id": "1"}], None))
    main._clear_cache()
    st._reset()
    main.get_cached_controllers()
    assert st.snapshot(60)[1]["state"] == "ok"
    monkeypatch.setattr(main, "_fetch_controllers", lambda: ([], "No data from AC Infinity"))
    main._clear_cache()
    main.get_cached_controllers()
    assert st.snapshot(60)[1]["last_error"] == "No data from AC Infinity"
