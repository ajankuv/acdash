"""request-safety: cross-site refusal, JSON content type, response headers."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeAPI, make_client

ROUTES = [
    ("post", "/api/port-control", {"dev_id": "1", "port": 1, "mode": "off"}),
    ("post", "/api/port-restore", {"dev_id": "1", "port": 1}),
    ("post", "/api/controller-stage", {"dev_id": "1", "stage": "Veg"}),
    ("post", "/api/automation-toggle", {"dev_id": "1", "adv_id": "a", "is_on": True}),
    ("delete", "/api/automation", {"dev_id": "1", "adv_id": "a"}),
    ("post", "/api/automation-create", {"dev_id": "1", "name": "n", "ports": [1]}),
]


@pytest.fixture
def app_env(monkeypatch):
    monkeypatch.setenv("ACDASH_USE_ENV_CREDENTIALS", "1")
    monkeypatch.setenv("ACINFINITY_EMAIL", "user@example.com")
    monkeypatch.setenv("ACINFINITY_PASSWORD", "right")
    for k in ("ACDASH_TRUSTED_ORIGINS", "ACDASH_FRAME_OPTIONS", "ACDASH_TRUST_PROXY_HEADERS"):
        monkeypatch.delenv(k, raising=False)
    import app.main as main
    import app.session as session
    import app.control as control

    api = FakeAPI()
    monkeypatch.setattr(session, "ACInfinityClient", lambda e, p: make_client(api, password=p, email=e))
    session.reset_client()
    main._clear_cache()
    control._reset_rate_limit()
    yield TestClient(main.app, base_url="http://dash.local:8080"), api
    session.reset_client()


def _writes(api):
    return sum(api.count(p) for p in ("/addDevMode", "/updateGroupsIsOn", "/delByid", "/addGroups"))


def test_cross_site_text_plain_port_control_refused(app_env):
    tc, api = app_env
    r = tc.post("/api/port-control", content='{"dev_id":"1","port":1,"mode":"off"}',
                headers={"Content-Type": "text/plain", "Origin": "http://evil.example",
                         "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert _writes(api) == 0 and api.count("/getdevModeSettingList") == 0


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_every_state_changing_route_refuses_foreign_origin(app_env, method, path, body):
    tc, api = app_env
    r = tc.request(method, path, json=body, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert _writes(api) == 0


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_fetch_metadata_refused(app_env, site):
    tc, _ = app_env
    r = tc.post("/api/controller-stage", json={"dev_id": "1", "stage": "Veg"}, headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403


def test_same_origin_ui_request_allowed(app_env):
    tc, _ = app_env
    r = tc.post("/api/controller-stage", json={"dev_id": "1", "stage": "Veg"},
                headers={"Origin": "http://dash.local:8080", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200


def test_script_without_browser_headers_allowed(app_env):
    tc, _ = app_env
    assert tc.post("/api/controller-stage", json={"dev_id": "1", "stage": "Veg"}).status_code == 200


def test_null_origin_refused(app_env):
    tc, _ = app_env
    r = tc.post("/api/controller-stage", json={"dev_id": "1", "stage": "Veg"}, headers={"Origin": "null"})
    assert r.status_code == 403


def test_trusted_origin_allowed(app_env, monkeypatch):
    tc, _ = app_env
    monkeypatch.setenv("ACDASH_TRUSTED_ORIGINS", "https://grow.example.com, https://other.example")
    r = tc.post("/api/controller-stage", json={"dev_id": "1", "stage": "Veg"},
                headers={"Origin": "https://grow.example.com"})
    assert r.status_code == 200


def test_forwarded_host_only_trusted_when_enabled(app_env, monkeypatch):
    tc, _ = app_env
    h = {"Origin": "https://proxy.example", "X-Forwarded-Host": "proxy.example"}
    assert tc.post("/api/controller-stage", json={"dev_id": "1", "stage": "Veg"}, headers=h).status_code == 403
    monkeypatch.setenv("ACDASH_TRUST_PROXY_HEADERS", "1")
    assert tc.post("/api/controller-stage", json={"dev_id": "1", "stage": "Veg"}, headers=h).status_code == 200


def test_setup_form_cross_site_refused(app_env, monkeypatch):
    tc, _ = app_env
    r = tc.post("/setup", data={"email": "x@y.z", "password": "p"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_json_routes_require_json_content_type(app_env, method, path, body):
    import json as _json
    tc, api = app_env
    r = tc.request(method, path, content=_json.dumps(body), headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    assert _writes(api) == 0


def test_json_body_must_be_object(app_env):
    tc, _ = app_env
    r = tc.post("/api/port-control", content="[1,2]", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_json_with_charset_accepted(app_env):
    tc, _ = app_env
    r = tc.post("/api/controller-stage", content='{"dev_id":"1","stage":"Veg"}',
                headers={"Content-Type": "application/json; charset=utf-8"})
    assert r.status_code == 200


def test_get_requests_unaffected_by_origin(app_env):
    tc, _ = app_env
    assert tc.get("/health", headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_default_security_headers(app_env):
    tc, _ = app_env
    r = tc.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    assert "x-frame-options" not in r.headers


@pytest.mark.parametrize("value,expected", [("DENY", "DENY"), ("sameorigin", "SAMEORIGIN"), ("bogus", None)])
def test_frame_options_opt_in(app_env, monkeypatch, value, expected):
    tc, _ = app_env
    monkeypatch.setenv("ACDASH_FRAME_OPTIONS", value)
    assert tc.get("/health").headers.get("x-frame-options") == expected
