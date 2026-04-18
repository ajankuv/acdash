"""QC: fetch live devices and verify every controller with T+RH also has VPD (API, sensor, or estimate).

Run from repo root:
  python -m app.qc_vpd
  python -m app.qc_vpd --strict   # exit 1 if any controller lacks VPD while having T+RH

Uses ACINFINITY_EMAIL / ACINFINITY_PASSWORD or ENV_FILE_PATH (default ./.env).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

from app.client import ACInfinityClient
from app.normalize import _pick_field, normalize_devices


def _load_creds() -> tuple[str, str]:
    env_path = Path(os.getenv("ENV_FILE_PATH", str(Path(__file__).resolve().parent.parent / ".env")))
    if env_path.is_file():
        vals = dotenv_values(env_path)
        email = (vals.get("ACINFINITY_EMAIL") or "").strip()
        password = (vals.get("ACINFINITY_PASSWORD") or "").strip()
        if email and password:
            return email, password
    email = (os.getenv("ACINFINITY_EMAIL") or "").strip()
    password = (os.getenv("ACINFINITY_PASSWORD") or "").strip()
    if not email or not password:
        print("Missing credentials: set ACINFINITY_EMAIL / ACINFINITY_PASSWORD or create .env", file=sys.stderr)
        sys.exit(2)
    return email, password


def _vpdnums_preview(device: dict, info: dict) -> str:
    raw = _pick_field(device, info, "vpdnums")
    if raw is None:
        raw = _pick_field(device, info, "vpdNums")
    return repr(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description="AC Dash VPD QC against live API")
    parser.add_argument("--strict", action="store_true", help="Exit 1 if VPD missing when T+RH present")
    args = parser.parse_args()

    email, password = _load_creds()
    client = ACInfinityClient(email, password)
    try:
        raw = client.get_devices()
    finally:
        client.close()

    if not raw:
        print("No devices returned (auth or API error).", file=sys.stderr)
        sys.exit(3)

    normalized = normalize_devices(raw)
    failures = 0
    print(f"Controllers: {len(normalized)}\n")
    print(f"{'ID':<18} {'Name':<18} {'T°C':>8} {'RH%':>6} {'VPD':>8} {'est?':>5}  vpdnums")
    print("-" * 96)

    for c, dev in zip(normalized, raw):
        tid = str(c["id"])
        name = (c["name"] or "")[:18]
        t = c["temp_c"]
        h = c["humidity_pct"]
        v = c["vpd_kpa"]
        est = "yes" if c["vpd_is_estimate"] else "no"
        info = dev.get("deviceInfo") or {}
        vp = _vpdnums_preview(dev, info if isinstance(info, dict) else {})
        ts = f"{t:.2f}" if t is not None else "—"
        hs = f"{h:.1f}" if h is not None else "—"
        vs = f"{v:.2f}" if v is not None else "—"
        print(f"{tid:<14} {name:<20} {ts:>8} {hs:>6} {vs:>8} {est:>5}  {vp}")
        if t is not None and h is not None and v is None:
            failures += 1
            raw_sensors = info.get("sensors") if isinstance(info, dict) else None
            if isinstance(raw_sensors, list):
                types = [s.get("sensorType") for s in raw_sensors if isinstance(s, dict)]
                print(f"  !! sensorTypes: {types}")

    print()
    if failures:
        print(f"FAIL: {failures} controller(s) had temp+RH but no VPD.")
        if args.strict:
            sys.exit(1)
    else:
        print("OK: every controller with T+RH has a VPD value.")


if __name__ == "__main__":
    main()
