#!/usr/bin/env python3
"""Smoke test the built acdash image against the fake AC Infinity API.

Stdlib only (runs on the CI host with any Python 3.9+). Phases:
  fresh    — no credentials: / redirects to setup, wrong password is rejected,
             correct password is saved, dashboard + APIs render fake controllers,
             a port write reaches the fake API.
  persist  — after a container restart: saved credentials and history survive.

Usage: smoke.py fresh|persist [--app http://127.0.0.1:18080] [--fake http://127.0.0.1:19000]
Exit code 0 = pass. Prints one line per check.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEV1 = "900000000000000001"
DEV2 = "900000000000000002"
FAILED: list[str] = []


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def req(method: str, url: str, *, data: bytes | None = None, ctype: str | None = None,
        timeout: float = 30) -> tuple[int, dict[str, str], str]:
    r = urllib.request.Request(url, data=data, method=method)
    if ctype:
        r.add_header("Content-Type", ctype)
    try:
        with _opener.open(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)
    return cond


def wait_for(url: str, seconds: float = 60) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if req("GET", url, timeout=3)[0] == 200:
                return True
        except OSError:
            pass
        time.sleep(1)
    return False


def form(d: dict[str, str]) -> bytes:
    return urllib.parse.urlencode(d).encode()


def dashboard_checks(app: str) -> None:
    st, _, body = req("GET", f"{app}/")
    check("GET / renders dashboard", st == 200 and "CI Flower Tent" in body and "CI Veg Tent" in body,
          f"status={st}")

    st, _, body = req("GET", f"{app}/api/dashboard-snapshot")
    ok = st == 200
    try:
        snap = json.loads(body)
        ok = ok and snap.get("error") in (None, "") and "Exhaust Fan" in (snap.get("cards_html") or "")
    except ValueError:
        ok = False
    check("GET /api/dashboard-snapshot has fake ports", ok, f"status={st} body={body[:200]}")

    st, _, body = req("GET", f"{app}/api/port-settings?dev_id={DEV1}&port=1")
    try:
        ps = json.loads(body)
    except ValueError:
        ps = {}
    check("GET /api/port-settings returns mode", st == 200 and "mode" in ps, f"status={st} body={body[:200]}")

    st, _, body = req("GET", f"{app}/api/history-chart?dev_id={DEV1}&hours=2")
    try:
        pts = json.loads(body).get("points") or []
    except ValueError:
        pts = []
    check("GET /api/history-chart returns points", st == 200 and len(pts) > 10, f"status={st} points={len(pts)}")

    st, _, _ = req("GET", f"{app}/api/automations?dev_id={DEV1}")
    check("GET /api/automations ok", st == 200, f"status={st}")

    st, _, body = req("GET", f"{app}/api/activity?dev_id={DEV1}")
    try:
        texts = [e["text"] for e in json.loads(body).get("events", [])]
    except ValueError:
        texts = []
    check("GET /api/activity decodes controller events", st == 200 and "Low water detected" in texts
          and "Port 1 mode: On (speed 5)" in texts and any(t.startswith("Unrecognized event") for t in texts)
          and not any("Cycle" in t for t in texts), f"status={st} texts={texts}")


def post_json(url: str, payload: dict) -> tuple[int, dict]:
    st, _, body = req("POST", url, data=json.dumps(payload).encode(), ctype="application/json")
    try:
        return st, json.loads(body)
    except ValueError:
        return st, {"raw": body[:200]}


def wait_verify(app: str, vid: str, seconds: float = 40) -> dict:
    deadline = time.time() + seconds
    last: dict = {}
    while time.time() < deadline:
        st, _, body = req("GET", f"{app}/api/port-control/verify?id={vid}")
        if st == 200:
            last = json.loads(body)
            if last.get("status") != "pending":
                return last
        time.sleep(2)
    return last


def write_verification_checks(app: str, fake: str) -> None:
    time.sleep(1.6)  # write spacing
    st, out = post_json(f"{app}/api/port-control", {"dev_id": DEV1, "port": 3, "mode": "manual", "state": True, "speed": 4})
    check("write returns pending + verify id", st == 200 and out.get("status") == "pending" and out.get("verify_id"), str(out))
    res = wait_verify(app, out.get("verify_id", ""))
    check("controller applied write → status applied", res.get("status") == "applied", str(res))

    time.sleep(1.6)
    st, out = post_json(f"{app}/api/port-control", {"dev_id": DEV1, "port": 3, "mode": "manual", "state": True, "speed": 4})
    check("identical write → no_change, nothing sent", out.get("status") == "no_change", str(out))

    req("POST", f"{fake}/__behavior", data=json.dumps({"ignore_writes": True}).encode(), ctype="application/json")
    time.sleep(1.6)
    st, out = post_json(f"{app}/api/port-control", {"dev_id": DEV1, "port": 3, "mode": "manual", "state": True, "speed": 9})
    res = wait_verify(app, out.get("verify_id", ""))
    check("device ignores write → status not_applied with hint", res.get("status") == "not_applied"
          and "ACINFINITY_WRITE_FORMAT" in (res.get("hint") or ""), str(res))
    req("POST", f"{fake}/__behavior", data=json.dumps({"ignore_writes": False}).encode(), ctype="application/json")

    st, _, body = req("GET", f"{app}/api/port-settings?dev_id={DEV1}&port=3")
    check("port-settings offers restore", json.loads(body).get("restore_available") is True, body[:200])
    time.sleep(1.6)
    st, out = post_json(f"{app}/api/port-restore", {"dev_id": DEV1, "port": 3})
    writes = json.loads(req("GET", f"{fake}/__writes")[2])
    last = {**writes[-1].get("query", {}), **writes[-1].get("form", {})}
    check("restore writes previous settings back", out.get("status") == "pending" and last.get("onSpead") == "4",
          f"{out} last_onSpead={last.get('onSpead')}")

    time.sleep(1.6)
    st, out = post_json(f"{app}/api/port-control", {"dev_id": DEV1, "port": 4, "mode": "manual", "state": True, "speed": 5})
    check("empty port (portResistance 65535) → clear error", st == 400 and "Nothing is plugged" in out.get("error", ""), str(out))


def phase_fresh(app: str, fake: str) -> None:
    check("app /health", wait_for(f"{app}/health"), "never healthy")
    check("fake /health", wait_for(f"{fake}/health"), "never healthy")
    req("POST", f"{fake}/__reset")

    st, hdr, _ = req("GET", f"{app}/")
    check("no creds: / redirects to /setup", st in (302, 303, 307) and "setup" in hdr.get("location", hdr.get("Location", "")),
          f"status={st}")
    st, _, body = req("GET", f"{app}/setup")
    check("GET /setup renders form", st == 200 and "password" in body.lower(), f"status={st}")

    st, _, body = req("POST", f"{app}/setup", data=form({"email": "ci@example.com", "password": "wrong"}),
                      ctype="application/x-www-form-urlencoded")
    check("wrong password rejected", st == 400 and "Incorrect Password" in body, f"status={st}")

    st, _, _ = req("POST", f"{app}/setup", data=form({"email": "ci@example.com", "password": "ci-password"}),
                   ctype="application/x-www-form-urlencoded")
    check("correct password saved", st in (302, 303), f"status={st}")

    dashboard_checks(app)
    for _ in range(3):
        time.sleep(2.5)  # CACHE_SECONDS=2 in the smoke stack → each refresh hits the fake API
        req("GET", f"{app}/api/dashboard-snapshot")
    calls = json.loads(req("GET", f"{fake}/__calls")[2])
    logins = calls.get("/api/user/appUserLogin", 0)
    # 2 wizard attempts (wrong + right, throwaway client) + 1 shared session login.
    check("session reused across requests (no login per request)", logins <= 4, f"logins={logins} calls={calls}")

    before = len(json.loads(req("GET", f"{fake}/__writes")[2]))
    st, _, body = req("POST", f"{app}/api/port-control",
                      data=json.dumps({"dev_id": DEV1, "port": 1, "mode": "manual", "state": True, "speed": 7}).encode(),
                      ctype="application/json")
    check("POST /api/port-control accepted", st == 200 and '"ok":true' in body.replace(" ", ""), f"status={st} body={body[:200]}")
    writes = json.loads(req("GET", f"{fake}/__writes")[2])
    last = writes[-1] if len(writes) > before else {}
    fields = {**last.get("query", {}), **last.get("form", {})}
    check("write reached fake API with full record", fields.get("onSpead") == "7" and fields.get("devId") == DEV1
          and fields.get("externalPort") == "1", f"writes={len(writes)} last={str(last)[:300]}")
    want = (os.environ.get("ACINFINITY_WRITE_FORMAT") or "query").strip().lower()
    ok_shape = (last.get("transport") == want) and (
        ("devSetting" not in fields and "modeSetid" not in fields) if want == "form" else "devSetting" in fields)
    check(f"write used the '{want}' format", ok_shape, f"transport={last.get('transport')} keys={sorted(fields)[:12]}")

    st, _, body = req("POST", f"{app}/api/port-control",
                      data=json.dumps({"dev_id": DEV1, "port": 99}).encode(), ctype="application/json")
    check("invalid port rejected with 400", st == 400, f"status={st}")

    write_verification_checks(app, fake)

    # Let the collector (5 s interval in the smoke stack) store at least one reading.
    time.sleep(12)


def phase_persist(app: str, fake: str) -> None:
    check("app /health after restart", wait_for(f"{app}/health"), "never healthy")
    st, _, body = req("GET", f"{app}/")
    check("saved credentials survive restart", st == 200 and "CI Flower Tent" in body, f"status={st}")
    dashboard_checks(app)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["fresh", "persist"])
    ap.add_argument("--app", default="http://127.0.0.1:18080")
    ap.add_argument("--fake", default="http://127.0.0.1:19000")
    a = ap.parse_args()
    {"fresh": phase_fresh, "persist": phase_persist}[a.phase](a.app.rstrip("/"), a.fake.rstrip("/"))
    print(f"\n{a.phase}: {'FAILED ' + str(len(FAILED)) + ' check(s)' if FAILED else 'all checks passed'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
