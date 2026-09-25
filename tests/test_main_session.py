"""Route-level session reuse: one login shared across requests; reset on new credentials."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeAPI, make_client


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ACDASH_USE_ENV_CREDENTIALS", "1")
    monkeypatch.setenv("ACINFINITY_EMAIL", "user@example.com")
    monkeypatch.setenv("ACINFINITY_PASSWORD", "right")
    import app.main as main
    import app.session as session
    import app.control as control

    api = FakeAPI()
    api.queue("/user/devInfoListAll", *[(200, {"code": 200, "data": [
        {"devId": "1", "devName": "Tent", "devType": 11, "deviceInfo": {"ports": []}}]})] * 50)
    monkeypatch.setattr(session, "ACInfinityClient",
                        lambda email, password: make_client(api, password=password, email=email))
    session.reset_client()
    main._clear_cache()
    control._reset_rate_limit()
    yield main, session, api
    session.reset_client()
    main._clear_cache()


def test_snapshot_refreshes_share_one_login(env):
    main, _session, api = env
    tc = TestClient(main.app)  # no lifespan → no background collector
    for _ in range(10):
        main._clear_cache()  # force a real fetch each time
        r = tc.get("/api/dashboard-snapshot")
        assert r.status_code == 200
    assert api.count("/user/devInfoListAll") == 10
    assert api.logins == 1


def test_routes_share_the_same_client(env):
    main, session, api = env
    tc = TestClient(main.app)
    tc.get("/api/dashboard-snapshot")
    tc.get("/api/port-settings?dev_id=1&port=1")
    tc.get("/api/automations?dev_id=1")
    assert api.logins == 1
    assert session.get_client("user@example.com", "right") is session.get_client("user@example.com", "right")


def test_changed_credentials_get_new_client(env):
    _main, session, _api = env
    a = session.get_client("user@example.com", "right")
    b = session.get_client("user@example.com", "other")
    assert a is not b
    assert session.get_client("user@example.com", "other") is b


def test_reset_client_forces_new_login(env):
    main, session, api = env
    tc = TestClient(main.app)
    tc.get("/api/dashboard-snapshot")
    session.reset_client()
    main._clear_cache()
    tc.get("/api/dashboard-snapshot")
    assert api.logins == 2


def test_setup_post_resets_shared_client(env, monkeypatch, tmp_path):
    main, session, api = env
    monkeypatch.delenv("ACDASH_USE_ENV_CREDENTIALS")
    monkeypatch.setattr(main, "ENV_FILE_PATH", tmp_path / ".env")
    monkeypatch.setattr(main, "ACInfinityClient", lambda e, p: make_client(api, password=p, email=e))
    old = session.get_client("user@example.com", "right")
    called = []
    monkeypatch.setattr(main, "reset_client", lambda: called.append(1))
    monkeypatch.setattr(main, "save_credentials_file", lambda e, p: None)
    tc = TestClient(main.app)
    r = tc.post("/setup", data={"email": "user@example.com", "password": "right"}, follow_redirects=False)
    assert r.status_code == 303
    assert called == [1]
    assert old is not None


def test_expired_write_asks_user_to_retry(env):
    main, _session, api = env
    tc = TestClient(main.app)
    api.queue("/dev/getdevModeSettingList", (200, {"code": 200, "data": {"atType": 1, "onSpead": 5}}))
    api.queue("/dev/addDevMode", (200, {"code": 10003, "msg": "Login Expired"}))
    r = tc.post("/api/port-control", json={"dev_id": "1", "port": 1, "mode": "manual", "state": True, "speed": 3})
    assert r.status_code == 400
    assert "apply the change again" in r.json()["error"]
    assert api.count("/dev/addDevMode") == 1


def test_bad_password_shows_sign_in_error(env, monkeypatch):
    main, session, api = env
    monkeypatch.setenv("ACINFINITY_PASSWORD", "wrong")
    session.reset_client()
    main._clear_cache()
    tc = TestClient(main.app)
    r = tc.get("/api/dashboard-snapshot")
    body = r.json()
    assert "Incorrect Password" in (body.get("error") or "")
    n = api.logins
    main._clear_cache()
    tc.get("/api/dashboard-snapshot")
    assert api.logins == n  # cool-down: no second login attempt
