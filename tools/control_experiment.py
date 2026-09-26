#!/usr/bin/env python3
"""Controlled port-write experiment for ONE allowlisted controller (the drying tent).

Why: AC Infinity answers 200 even when a controller ignores a write, and published write
recipes disagree across app versions. This tool changes only the power level of one port,
once per write variant, and checks the controller's LIVE speed (devInfoListAll) — then puts
the port back exactly as it was. The dashboard itself never uses these variants.

Safety rails (all enforced, not warnings):
- Target must match BOTH ACDASH_EXPERIMENT_DEV_ID and ACDASH_EXPERIMENT_DEV_NAME (checked
  against the live device list) — any other controller is refused before any write.
- Dry run by default (reads only). Writes need --live plus typing the controller's name.
- Refuses empty ports, shared controllers, ports under Advance Automation, and ports that
  aren't already in On mode.
- Saves the port's full settings first; restores after EVERY variant and on any exit
  (errors, Ctrl-C). If a plain restore isn't confirmed live, it bounces the mode (Off, then
  the original settings) — mode changes are known to apply — and reports loudly if even that
  can't be confirmed.

Usage:
  export ACDASH_EXPERIMENT_DEV_ID=...  ACDASH_EXPERIMENT_DEV_NAME="Drying"   # local only
  export ACINFINITY_EMAIL=... ACINFINITY_PASSWORD=...     # or put them in RND/.env
  python tools/control_experiment.py                       # list the dry tent's ports
  python tools/control_experiment.py --port 2              # dry run: show the plan
  python tools/control_experiment.py --port 2 --live       # run it (asks for confirmation)
Reports go to RND/experiments/ (gitignored).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app import control  # noqa: E402
from app.control import ControlError, RateLimitError, WriteVariant  # noqa: E402
from app.verify import live_port, port_matches  # noqa: E402

AT_OFF, AT_ON = 1, 2


def _plain_http_base() -> str:
    """The configured API base with http instead of https — never a hard-coded real host,
    so runs against the CI fake API can't reach the real cloud."""
    from app.client import API_BASE
    return API_BASE.replace("https://", "http://", 1)


PLAIN_HTTP_BASE = _plain_http_base()

BASELINE = WriteVariant(name="baseline (current default)")
SINGLE_KNOBS = [
    WriteVariant(name="onlyUpdateSpeed=1", only_update_speed=1),
    WriteVariant(name="clamp °F >= 32", clamp_f=True),
    WriteVariant(name="modeType=2", force_mode_type=2),
    WriteVariant(name="form body", fmt="form"),
    WriteVariant(name="signed", sign=True),
    WriteVariant(name="app headers (devType, empty minversion)", app_headers=True),
    WriteVariant(name="plain http", api_base=PLAIN_HTTP_BASE),
]
COMBOS = [
    WriteVariant(name="app 2.0.0 recipe", only_update_speed=1, clamp_f=True, app_headers=True),
    WriteVariant(name="Backroads4Me 2.0.8 recipe", fmt="form", sign=True, app_headers=True, clamp_f=True),
    WriteVariant(name="everything", only_update_speed=1, clamp_f=True, force_mode_type=2, fmt="form",
                 sign=True, app_headers=True),
]


class Refused(Exception):
    """Safety rail tripped — nothing was (or will be) written."""


@dataclass
class Result:
    variant: str
    knobs: dict[str, Any]
    kind: str
    target: dict[str, Any]
    outcome: str = "pending"          # applied | ignored | error
    seconds_to_apply: float | None = None
    response: str | None = None
    timeline: list[dict[str, Any]] = field(default_factory=list)
    restore: str | None = None        # confirmed | confirmed-after-bounce | NOT CONFIRMED


def load_local_env() -> None:
    """Pick up credentials from RND/.env (gitignored) without overriding the shell."""
    env = REPO / "RND" / ".env"
    if env.is_file():
        try:
            from dotenv import load_dotenv
            load_dotenv(env, override=False)
        except ImportError:
            pass


def allowlist_from_env() -> tuple[str, str]:
    dev_id = (os.environ.get("ACDASH_EXPERIMENT_DEV_ID") or "").strip()
    name = (os.environ.get("ACDASH_EXPERIMENT_DEV_NAME") or "").strip()
    if not dev_id or not name:
        raise Refused("ACDASH_EXPERIMENT_DEV_ID and ACDASH_EXPERIMENT_DEV_NAME must both be set "
                      "(the drying tent's device ID and part of its name). Nothing was contacted.")
    return dev_id, name


class Experiment:
    def __init__(self, client: Any, allow_id: str, allow_name: str, port: int | None, *,
                 window: float = 150.0, poll: float = 10.0, write_gap: float = 5.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 out: Callable[[str], None] = print) -> None:
        self.client = client
        self._allow_id = str(allow_id)
        self.allow_name = allow_name
        self.port = port
        self.window, self.poll, self.write_gap = window, poll, write_gap
        self.sleep, self.clock, self.out = sleep, clock, out
        self.device: dict[str, Any] = {}
        self.original: dict[str, Any] = {}
        self.original_live: dict[str, Any] = {}
        self.results: list[Result] = []
        self.restore_failed = False
        self._last_write = -1e9

    # ── safety ──────────────────────────────────────────────────────
    def check_target(self) -> dict[str, Any]:
        devices = self.client.get_devices()
        dev = next((d for d in devices if str(d.get("devId")) == self._allow_id), None)
        if dev is None:
            raise Refused("The allowlisted controller is not on this account (or the device list is empty).")
        name = str(dev.get("devName") or "")
        if self.allow_name.lower() not in name.lower():
            raise Refused(f"Controller name {name!r} does not contain {self.allow_name!r} — refusing.")
        if str(dev.get("isShare", "0")) == "1":
            raise Refused("Shared controller — refusing.")
        self.device = dev
        return dev

    def ports(self) -> list[dict[str, Any]]:
        return list((self.device.get("deviceInfo") or {}).get("ports") or [])

    def check_port(self) -> None:
        if self.port is None:
            raise Refused("Choose a port with --port.")
        rec = next((p for p in self.ports() if int(p.get("port", -1)) == int(self.port)), None)
        if rec is None:
            raise Refused(f"Port {self.port} does not exist on this controller.")
        if int(rec.get("portResistance", 0) or 0) == 65535:
            raise Refused(f"Port {self.port}: nothing is plugged in.")
        raw = control._raw_record(self.client.get_dev_mode_setting_list(self._allow_id, int(self.port)))
        if not raw:
            raise Refused("Could not read the port's settings — refusing.")
        if int(raw.get("isOpenAutomation") or 0) == 1:
            raise Refused(f"Port {self.port} is under an Advance Automation — refusing.")
        if int(raw.get("atType") or 0) != AT_ON:
            raise Refused(f"Port {self.port} is not in On mode (atType={raw.get('atType')}). "
                          "Set it to On in the app first, so the test only changes the power level.")
        self.original = raw
        self.original_live = dict(rec)

    def _guard(self) -> str:
        """Every write re-checks: the target must be the device verified by check_target()
        (ID and name) and must still equal the allowlisted ID. Returns the ID to write to."""
        verified = str(self.device.get("devId")) if self.device else ""
        if not verified or verified != self._allow_id or self.allow_name.lower() not in str(
                self.device.get("devName") or "").lower():
            raise Refused("Write blocked: target controller has not been verified as the allowlisted one.")
        return verified

    # ── primitives ──────────────────────────────────────────────────
    def _write(self, changes: dict[str, Any], *, variant: WriteVariant | None = None,
               restore_record: dict[str, Any] | None = None) -> dict[str, Any]:
        dev_id = self._guard()
        wait = self._last_write + self.write_gap - self.clock()
        if wait > 0:
            self.sleep(wait)
        control._reset_rate_limit()  # the tool paces itself (write_gap >= the app's 1.5 s)
        try:
            return control.write_port_control(self.client, dev_id, int(self.port), changes,
                                              restore_record=restore_record, variant=variant)
        finally:
            self._last_write = self.clock()

    def live(self) -> dict[str, Any] | None:
        try:
            return live_port(self.client.get_devices(), self._allow_id, int(self.port))
        except Exception:  # noqa: BLE001 — a missed poll isn't fatal
            return None

    def watch(self, done: Callable[[dict[str, Any]], bool], timeline: list[dict[str, Any]]) -> float | None:
        start = self.clock()
        while True:
            lp = self.live()
            t = round(self.clock() - start, 1)
            if lp is not None:
                timeline.append({"t": t, "speed": lp.get("speak"), "mode": lp.get("curMode"),
                                 "load": lp.get("loadState")})
                if done(lp):
                    return t
            if self.clock() - start + self.poll > self.window:
                return None
            self.sleep(self.poll)

    def _is_original(self, lp: dict[str, Any]) -> bool:
        return port_matches(lp, {"atType": AT_ON, "speed": int(self.original_live.get("speak") or 0)})

    def restore(self) -> str:
        """Put the port back; confirm from live state; bounce the mode if needed."""
        tl: list[dict[str, Any]] = []
        try:
            self._write({}, restore_record=self.original)
        except (ControlError, RateLimitError) as e:
            self.out(f"  restore write failed: {e}")
        if self.watch(self._is_original, tl) is not None:
            return "confirmed"
        self.out("  plain restore not confirmed — bouncing the mode (Off, then original settings)")
        try:
            self._write({"mode": "off"})
            self.watch(lambda lp: port_matches(lp, {"atType": AT_OFF, "speed": 0}), tl)
            self._write({}, restore_record=self.original)
        except (ControlError, RateLimitError) as e:
            self.out(f"  bounce write failed: {e}")
        if self.watch(self._is_original, tl) is not None:
            return "confirmed-after-bounce"
        self.restore_failed = True
        return "NOT CONFIRMED"

    # ── variants ────────────────────────────────────────────────────
    def target_speed(self) -> int:
        base = int(self.original.get("onSpead") or self.original_live.get("speak") or 5)
        return base + 2 if base <= 8 else base - 2

    def run_speed_variant(self, v: WriteVariant) -> Result:
        target = self.target_speed()
        r = Result(v.name, v.knobs(), "speed", {"speed": target})
        self.out(f"• {v.name}: power {self.original.get('onSpead')} → {target}")
        try:
            resp = self._write({"mode": "manual", "state": True, "speed": target}, variant=v)
            r.response = f"accepted ({resp.get('format')})"
            took = self.watch(lambda lp: int(lp.get("speak") or -1) == target, r.timeline)
            r.outcome, r.seconds_to_apply = ("applied", took) if took is not None else ("ignored", None)
        except (ControlError, RateLimitError) as e:
            r.outcome, r.response = "error", str(e)
        self.out(f"  → {r.outcome}" + (f" in {r.seconds_to_apply}s" if r.seconds_to_apply is not None else ""))
        r.restore = self.restore()
        self.out(f"  restore: {r.restore}")
        self.results.append(r)
        return r

    def run_mode_control(self) -> Result:
        r = Result("mode change control (On → Off)", {}, "mode", {"mode": "off"})
        self.out(f"• {r.variant}")
        try:
            self._write({"mode": "off"})
            r.response = "accepted"
            took = self.watch(lambda lp: port_matches(lp, {"atType": AT_OFF, "speed": 0}), r.timeline)
            r.outcome, r.seconds_to_apply = ("applied", took) if took is not None else ("ignored", None)
        except (ControlError, RateLimitError) as e:
            r.outcome, r.response = "error", str(e)
        self.out(f"  → {r.outcome}")
        r.restore = self.restore()
        self.out(f"  restore: {r.restore}")
        self.results.append(r)
        return r

    def run(self, only: set[str] | None = None) -> None:
        def wanted(v: WriteVariant) -> bool:
            return not only or v.name in only
        if wanted(BASELINE):
            self.run_speed_variant(BASELINE)
        if not only or "mode change control" in only:
            self.run_mode_control()
        for v in SINGLE_KNOBS:
            if wanted(v) and not self.restore_failed:
                self.run_speed_variant(v)
        winners = [r for r in self.results if r.kind == "speed" and r.outcome == "applied"
                   and r.variant != BASELINE.name]
        if not self.restore_failed:
            if winners:  # confirm the first winner reproduces
                v = next(x for x in SINGLE_KNOBS if x.name == winners[0].variant)
                self.run_speed_variant(replace(v, name=v.name + " (repeat)"))
            else:
                for v in COMBOS:
                    if wanted(v) and not self.restore_failed:
                        self.run_speed_variant(v)

    # ── plan (dry run) ─────────────────────────────────────────────
    def plan(self) -> list[dict[str, Any]]:
        target = self.target_speed()
        current = control.normalize_port_settings([self.original])
        overlay = control.build_mode_payload(self._allow_id, int(self.port), current,
                                             {"mode": "manual", "state": True, "speed": target})
        dev_type = self.device.get("devType")
        base_payload = control.build_write_payload(self.original, overlay, control.get_write_format(), dev_type=dev_type)
        rows = []
        for v in [BASELINE, *SINGLE_KNOBS, *COMBOS]:
            fmt = v.fmt or control.get_write_format()
            payload = control.apply_variant(control.build_write_payload(self.original, overlay, fmt, dev_type=dev_type), v)
            changed = sorted(k for k in payload if payload.get(k) != base_payload.get(k))
            headers = ["token", "User-Agent"] + (["sign", "requestApp", "requestId", "version"] if v.sign else []) \
                + (["devType", "minversion"] if v.app_headers else [])
            rows.append({"variant": v.name, "transport": fmt, "fields": len(payload),
                         "changed_vs_baseline": changed, "headers": headers,
                         "host": (v.api_base or "default (https)")})
        return rows

    # ── report ──────────────────────────────────────────────────────
    def report(self, secrets: list[str]) -> dict[str, Any]:
        speed = [r for r in self.results if r.kind == "speed"]
        base = next((r for r in speed if r.variant == BASELINE.name), None)
        mode = next((r for r in self.results if r.kind == "mode"), None)
        rep = {
            "device": {"name": self.device.get("devName"), "devType": self.device.get("devType"),
                       "firmware": self.device.get("firmwareVersion"), "id_suffix": self._allow_id[-4:]},
            "port": self.port,
            "original": {"atType": self.original.get("atType"), "onSpead": self.original.get("onSpead"),
                         "live_speed": self.original_live.get("speak")},
            "reproduced_problem": bool(base and base.outcome == "ignored" and mode and mode.outcome == "applied"),
            "applied_variants": [r.variant for r in speed if r.outcome == "applied"],
            "restore_failed": self.restore_failed,
            "results": [r.__dict__ for r in self.results],
        }
        return scrub(rep, secrets)


def scrub(obj: Any, secrets: list[str]) -> Any:
    text = json.dumps(obj, default=str)
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "<redacted>")
    return json.loads(text)


def write_report(rep: dict[str, Any], report_dir: Path) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    jpath, mpath = report_dir / f"{stamp}-control-experiment.json", report_dir / f"{stamp}-control-experiment.md"
    jpath.write_text(json.dumps(rep, indent=2))
    lines = [f"# Control experiment — {rep['device']['name']} port {rep['port']}", "",
             f"- Reproduced the reported problem: **{rep['reproduced_problem']}**",
             f"- Variants the controller applied: **{', '.join(rep['applied_variants']) or 'none'}**",
             f"- Restore failed at any point: **{rep['restore_failed']}**", "",
             "| Variant | Outcome | Seconds | Response | Restore |", "|---|---|---|---|---|"]
    for r in rep["results"]:
        lines.append(f"| {r['variant']} | {r['outcome']} | {r['seconds_to_apply'] or ''} | {r['response'] or ''} | {r['restore']} |")
    mpath.write_text("\n".join(lines) + "\n")
    return jpath, mpath


def main(argv: list[str] | None = None, *, client_factory: Callable[[str, str], Any] | None = None,
         input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> int:
    ap = argparse.ArgumentParser(description="Dry-tent-only port write experiment")
    ap.add_argument("--port", type=int)
    ap.add_argument("--live", action="store_true", help="actually send writes (asks for confirmation)")
    ap.add_argument("--confirm", help="controller name, to confirm non-interactively")
    ap.add_argument("--window", type=float, default=150.0)
    ap.add_argument("--poll", type=float, default=10.0)
    ap.add_argument("--write-gap", type=float, default=5.0)
    ap.add_argument("--only", help="comma-separated variant names to run")
    ap.add_argument("--report-dir", default=str(REPO / "RND" / "experiments"))
    a = ap.parse_args(argv)

    load_local_env()
    # The app's write path snapshots every pre-write record to SQLite; keep the tool's own
    # snapshots next to its reports (gitignored) instead of the dashboard's history DB.
    from app import storage
    Path(a.report_dir).mkdir(parents=True, exist_ok=True)
    storage.DB_PATH = str(Path(a.report_dir) / "experiment-snapshots.db")
    storage.init_db()
    try:
        allow_id, allow_name = allowlist_from_env()
    except Refused as e:
        out(f"REFUSED: {e}")
        return 2
    email = (os.environ.get("ACINFINITY_EMAIL") or "").strip()
    password = (os.environ.get("ACINFINITY_PASSWORD") or "").strip()
    if not email or not password:
        out("REFUSED: set ACINFINITY_EMAIL and ACINFINITY_PASSWORD (shell or RND/.env).")
        return 2
    if client_factory is None:
        from app.client import ACInfinityClient
        client_factory = ACInfinityClient
    client = client_factory(email, password)
    exp = Experiment(client, allow_id, allow_name, a.port, window=a.window, poll=a.poll,
                     write_gap=a.write_gap, sleep=sleep, clock=clock, out=out)
    try:
        dev = exp.check_target()
        out(f"Controller: {dev.get('devName')} (devType {dev.get('devType')}, fw {dev.get('firmwareVersion')})")
        if a.port is None:
            for p in exp.ports():
                out(f"  port {p.get('port')}: {p.get('portName')} speed={p.get('speak')} mode={p.get('curMode')} "
                    f"{'(empty)' if int(p.get('portResistance', 0) or 0) == 65535 else ''}")
            out("Pick a port that is in On mode with a device plugged in, then pass --port N.")
            return 0
        exp.check_port()
        out(f"Port {a.port}: On at power {exp.original.get('onSpead')} (live {exp.original_live.get('speak')}); "
            f"each variant sets power {exp.target_speed()} then restores.")
        for row in exp.plan():
            out(f"  - {row['variant']}: {row['transport']}, {row['fields']} fields, changed {row['changed_vs_baseline']}, "
                f"headers {row['headers']}, host {row['host']}")
        if not a.live:
            out("DRY RUN — nothing was written. Re-run with --live to execute.")
            return 0
        typed = a.confirm if a.confirm is not None else input_fn(f"Type the controller name ({dev.get('devName')}) to start: ")
        if (typed or "").strip().lower() != str(dev.get("devName") or "").strip().lower():
            out("REFUSED: confirmation did not match the controller name. Nothing was written.")
            return 2
        snap = Path(a.report_dir)
        snap.mkdir(parents=True, exist_ok=True)
        (snap / f"{time.strftime('%Y%m%d-%H%M%S')}-original-port{a.port}.json").write_text(
            json.dumps(scrub(exp.original, [client.token or "", email, password]), indent=2))
        try:
            exp.run(set(s.strip() for s in a.only.split(",")) if a.only else None)
        except KeyboardInterrupt:
            out("Interrupted — restoring the port before exiting…")
            out(f"  restore: {exp.restore()}")
    except Refused as e:
        out(f"REFUSED: {e}")
        return 2
    rep = exp.report([client.token or "", email, password])
    jpath, mpath = write_report(rep, Path(a.report_dir))
    out(f"SUMMARY reproduced_problem={rep['reproduced_problem']} applied={rep['applied_variants']} "
        f"restore_failed={rep['restore_failed']}")
    out(f"Report: {mpath}")
    if exp.restore_failed:
        out("!!! RESTORE NOT CONFIRMED — the original settings are saved in the report folder; re-apply them.")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
