"""HTTP smoke + OpenAPI checks for AC Dash (run after deploy).

Examples:
  python -m app.qc_http
  python -m app.qc_http --base http://127.0.0.1:8080
  docker exec acdash python -m app.qc_http --base http://127.0.0.1:8080

Exit 0 only if:
  - GET /health is 200 and body is OK
  - OpenAPI lists /api/dashboard-snapshot and /dashboard-snapshot
  - GET /api/dashboard-snapshot is NOT 404 (401 without creds is OK; 200 with creds is OK)

If OpenAPI shows only /, /health, /setup you are hitting an OLD image or another process on the port.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _get(url: str, *, accept: str | None = None) -> tuple[int, bytes]:
    headers = {}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> None:
    parser = argparse.ArgumentParser(description="AC Dash HTTP / OpenAPI QC")
    parser.add_argument(
        "--base",
        default=os.environ.get("ACDASH_QC_BASE", "http://127.0.0.1:8080"),
        help="Base URL (no trailing slash)",
    )
    args = parser.parse_args()
    base = args.base.rstrip("/")
    failures = 0

    code, body = _get(f"{base}/health")
    if code != 200 or body.decode().strip() != "OK":
        print(f"FAIL /health: status={code!r} body={body[:80]!r}")
        failures += 1
    else:
        print("OK /health")

    code, body = _get(f"{base}/openapi.json", accept="application/json")
    if code != 200:
        print(f"FAIL /openapi.json: status={code!r}")
        failures += 1
        sys.exit(1 if failures else 0)

    try:
        spec = json.loads(body.decode())
    except json.JSONDecodeError as e:
        print(f"FAIL /openapi.json: invalid JSON: {e}")
        sys.exit(1)

    paths = set(spec.get("paths") or [])
    title = (spec.get("info") or {}).get("title", "")
    if title != "AC Dash":
        print(f"WARN: OpenAPI title is {title!r} (expected 'AC Dash') — wrong service?")

    for need in ("/api/dashboard-snapshot", "/dashboard-snapshot"):
        if need not in paths:
            print(f"FAIL OpenAPI missing {need!r}")
            print(f"     This usually means an OLD acdash image (only has /, /health, /setup).")
            print(f"     Redeploy: docker pull/build acdash:latest && recreate the container.")
            print(f"     Paths seen: {sorted(paths)}")
            failures += 1
    if not failures:
        print("OK OpenAPI snapshot routes present")

    code, _ = _get(f"{base}/api/dashboard-snapshot", accept="application/json")
    if code == 404:
        print("FAIL GET /api/dashboard-snapshot returned 404 — route not mounted on this process.")
        failures += 1
    elif code not in (200, 401):
        print(f"WARN GET /api/dashboard-snapshot status={code} (expected 200 or 401)")
    else:
        print(f"OK GET /api/dashboard-snapshot status={code}")

    if failures:
        sys.exit(1)
    print("QC HTTP: all checks passed.")


if __name__ == "__main__":
    main()
