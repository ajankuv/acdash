"""Session handling in app/client.py: expiry classification, re-login, rate limits, login rules."""
from __future__ import annotations

import threading
import urllib.parse

import httpx
import pytest

from app import client as client_mod
from app.client import ACInfinityClient, _classify, _login_attempt_variants
from tests.fakes import FakeAPI, make_client

@pytest.fixture
def api():
    return FakeAPI()


# ── classification ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, {"code": 200}, "ok"),
        (401, None, "expired"),
        (200, {"code": 10003, "msg": "Login Expired"}, "expired"),
        (403, {"code": 403, "msg": "Login Expired"}, "expired"),
        (200, {"code": 403, "msg": "Please login again"}, "expired"),
        (403, {"code": 403, "msg": "Data saving failed"}, "error"),
        (200, {"code": 999998, "msg": "Rate Limiting!"}, "rate_limited"),
        (200, {"code": 999999, "msg": "Operation failed"}, "error"),
        (500, None, "error"),
    ],
)
def test_classify(status, body, expected):
    assert _classify(status, body) == expected


# ── expiry recovery ────────────────────────────────────────────────


def test_expired_read_relogs_in_and_retries_once(api):
    c = make_client(api)
    api.queue("/user/devInfoListAll", (200, {"code": 10003, "msg": "Login Expired"}),
              (200, {"code": 200, "data": [{"devId": "1", "devType": 11}]}))
    devices = c.get_devices()
    assert devices == [{"devId": "1", "devType": 11}]
    assert api.logins == 2  # initial + renewal
    assert api.count("/user/devInfoListAll") == 2
    # userId in the retried body follows the renewed token
    assert api.calls[-1][2]["userId"] == "tok-2"


def test_expired_read_gives_up_after_one_retry(api):
    c = make_client(api)
    api.queue("/dev/getdevModeSettingList", *[(200, {"code": 10003, "msg": "Login Expired"})] * 3)
    body = c.get_dev_mode_setting_list("1", 1)
    assert body["code"] == 10003
    assert api.count("/dev/getdevModeSettingList") == 2


def test_expired_write_renews_but_does_not_resend(api):
    c = make_client(api)
    api.queue("/dev/addDevMode", (403, {"code": 403, "msg": "Login Expired"}))
    result = c.set_port_mode("1", 1, {"atType": 2})
    assert result["msg"] == "Login Expired"
    assert api.count("/dev/addDevMode") == 1
    assert api.logins == 2  # session renewed for the user's retry
    assert c.token == "tok-2"


@pytest.mark.parametrize("method,args", [
    ("toggle_automation_raw", ("1", "a", ), ),
    ("delete_automation_raw", ("1", "a")),
    ("create_automation_raw", ("1", {"advName": "x"})),
])
def test_automation_writes_not_resent_on_expiry(api, method, args):
    c = make_client(api)
    for p in ("/updateGroupsIsOn", "/delByid", "/addGroups"):
        api.queue(p, (200, {"code": 10003, "msg": "Login Expired"}))
    kwargs = {"is_on": True} if method == "toggle_automation_raw" else {}
    out = getattr(c, method)(*args, **kwargs)
    assert out["code"] == 10003
    writes = sum(api.count(p) for p in ("/updateGroupsIsOn", "/delByid", "/addGroups"))
    assert writes == 1


def test_http_401_still_handled(api):
    c = make_client(api)
    api.queue("/dev/getDevSetting", (401, {}), (200, {"code": 200, "data": {"x": 1}}))
    assert c.get_dev_setting("1", 0)["data"] == {"x": 1}


def test_concurrent_expired_reads_login_once(api):
    c = make_client(api)
    assert c.authenticate()
    api.logins = 0
    api.expired_tokens.add(c.token)  # server expires the current session; renewed tokens work
    barrier = threading.Barrier(5)
    results = []

    def work():
        barrier.wait()
        results.append(c.get_dev_setting("1", 0))

    threads = [threading.Thread(target=work) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 5
    assert all(r and r.get("code") == 200 for r in results)
    assert api.logins == 1


# ── login rules ────────────────────────────────────────────────────


def test_short_password_variants_unchanged():
    assert _login_attempt_variants("a@b.c", "p" * 20) == [("a@b.c", "p" * 20)]


def test_long_password_adds_truncated_variant_last():
    pw = "x" * 30
    rows = _login_attempt_variants("A@b.c", pw)
    assert rows[0] == ("A@b.c", pw)
    assert rows[-1][1] == "x" * 25
    assert all(len(p) == 30 for _, p in rows[:-2])  # originals first


def test_long_password_logs_in_with_truncation(api):
    api.password = "y" * 25
    c = make_client(api, password="y" * 30)
    assert c.authenticate()
    assert c.token


def test_login_cooldown_after_refusal(api):
    c = make_client(api, password="wrong")
    now = [1000.0]
    c._monotonic = lambda: now[0]
    assert not c.authenticate()
    assert "Incorrect Password" in (c.last_auth_error or "")
    n = api.logins
    assert not c.authenticate()
    assert c.get_devices() == []
    assert api.logins == n  # no login during cool-down
    assert "Incorrect Password" in (c.last_auth_error or "")
    now[0] += client_mod.LOGIN_COOLDOWN_SECS + 1
    c.authenticate()
    assert api.logins > n


def test_network_failure_does_not_start_cooldown():
    def boom(request):
        raise httpx.ConnectError("down")

    c = ACInfinityClient("u@e.c", "p")
    c._client = httpx.Client(transport=httpx.MockTransport(boom))
    assert not c.authenticate()
    assert c._login_blocked_until == 0.0


def test_login_stores_signing_fields(api):
    c = make_client(api)
    assert c.authenticate()
    assert (c.secret_id, c.request_app) == ("s", "r")


# ── rate limiting ──────────────────────────────────────────────────


def test_rate_limit_backs_off_and_retries_once(api):
    c = make_client(api)
    api.queue("/dev/getDevSetting", (200, {"code": 999998, "msg": "Rate Limiting!"}),
              (200, {"code": 200, "data": {"ok": 1}}))
    assert c.get_dev_setting("1", 0)["data"] == {"ok": 1}
    assert client_mod.RATE_LIMIT_BACKOFF_SECS in c.slept


def test_history_rate_limited_twice_reports_error(api):
    c = make_client(api)
    api.queue("/log/dataPage", *[(200, {"code": 999998, "msg": "Rate Limiting!"})] * 2)
    assert c.history_data_page("1", 2000, 1000) is None
    assert api.count("/log/dataPage") == 2
    assert "rate limiting" in (c.last_request_error or "").lower()


def test_log_calls_are_spaced(api):
    c = make_client(api)
    now = [0.0]
    c._monotonic = lambda: now[0]

    def fake_sleep(s):
        c.slept.append(s)
        now[0] += s

    c._sleep = fake_sleep
    api.queue("/log/dataPage", *[(200, {"code": 200, "data": {"rows": []}})] * 2)
    c.history_data_page("1", 2000, 1000)
    now[0] += 1.0
    c.history_data_page("1", 2000, 1000)
    assert any(abs(s - 1.5) < 1e-6 for s in c.slept)


# ── AI+ headers ────────────────────────────────────────────────────


def test_v2_headers_standard_controller_unchanged(api):
    c = make_client(api)
    api.queue("/user/devInfoListAll", (200, {"code": 200, "data": [{"devId": "1", "devType": 11}]}))
    c.get_devices()
    h = c._v2_headers("1")
    assert h["devType"] == "11" and h["minversion"] == "0.0.0"


def test_v2_headers_ai_controller(api):
    c = make_client(api)
    api.queue("/user/devInfoListAll", (200, {"code": 200, "data": [{"devId": "9", "devType": 20}]}))
    c.get_devices()
    c.get_automations_raw("9")
    sent = [h for p, _, _, h in api.calls if p.endswith("/getGroups")][-1]
    assert sent["devtype"] == "20" and sent["minversion"] == "3.5"


# ── review fixes ───────────────────────────────────────────────────


def test_rate_limited_write_is_not_resent(api):
    c = make_client(api)
    api.queue("/dev/addDevMode", (200, {"code": 999998, "msg": "Rate Limiting!"}))
    out = c.set_port_mode("1", 1, {"atType": 2})
    assert out["code"] == 999998
    assert api.count("/dev/addDevMode") == 1
    assert c.slept == []  # no 10 s wait while holding the write lock


def test_closed_client_returns_none_instead_of_raising(api):
    c = make_client(api)
    assert c.authenticate()
    c._client.close()
    assert c.get_dev_setting("1", 0) is None


def test_stale_rate_limit_message_cleared(api):
    c = make_client(api)
    api.queue("/log/dataPage", *[(200, {"code": 999998, "msg": "Rate Limiting!"})] * 2)
    assert c.history_data_page("1", 2000, 1000) is None
    assert c.last_request_error
    api.queue("/log/dataPage", (200, {"code": 500, "msg": "server error"}))
    assert c.history_data_page("1", 2000, 1000) is None
    assert c.last_request_error is None


def test_reset_client_defers_close(monkeypatch):
    import app.session as session
    closed = []
    monkeypatch.setattr(session, "RETIRE_DELAY_SECS", 0.05)
    c = session.get_client("a@b.c", "one")
    monkeypatch.setattr(c, "close", lambda: closed.append(1))
    session.reset_client()
    assert closed == []  # still usable by in-flight requests
    import time as _t
    _t.sleep(0.2)
    assert closed == [1]


def test_logged_errors_never_contain_query_secrets(caplog):
    def boom(request):
        raise httpx.ConnectError(f"failed for {request.url}")

    c = ACInfinityClient("user@example.com", "pw-test-only")
    c._client = httpx.Client(transport=httpx.MockTransport(boom))
    with caplog.at_level("DEBUG"):
        c.authenticate()                      # query-string login variant puts the password in the URL
        c.token = "tok-test"
        c.history_data_page("1", 2000, 1000)  # history puts appId=<token> in the URL
    assert "pw-test-only" not in caplog.text
    assert "tok-test" not in caplog.text
    assert "redacted" in caplog.text


def test_httpx_request_logging_silenced():
    import logging
    import app.main  # noqa: F401 — configures logging
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
