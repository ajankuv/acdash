"""tools/control_experiment.py — safety rails and measurement, against the CI fake API."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import app.control as control
from app.client import ACInfinityClient

REPO = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fake = _load("fake_acinfinity_server", REPO / "ci" / "fake_acinfinity" / "server.py")
tool = _load("control_experiment", REPO / "tools" / "control_experiment.py")

DEV = "900000000000000001"   # fake "CI Flower Tent": port 1 On@5, port 2 Off, port 4 empty
OTHER = "900000000000000002"


class Clock:
    def __init__(self):
        self.t = 0.0
        self.interrupt_at = None
        self.calls = 0

    def now(self):
        return self.t

    def sleep(self, s):
        self.calls += 1
        if self.interrupt_at is not None and self.calls == self.interrupt_at:
            raise KeyboardInterrupt
        self.t += s


@pytest.fixture
def env(monkeypatch, tmp_path):
    fake.S.reset()
    control._reset_rate_limit()
    monkeypatch.setenv("ACDASH_EXPERIMENT_DEV_ID", DEV)
    monkeypatch.setenv("ACDASH_EXPERIMENT_DEV_NAME", "Flower")
    monkeypatch.setenv("ACINFINITY_EMAIL", "ci@example.com")
    monkeypatch.setenv("ACINFINITY_PASSWORD", "ci-password")
    monkeypatch.setattr(tool, "load_local_env", lambda: None)
    made = []

    def factory(e, p):
        c = ACInfinityClient(e, p)
        c._client.close()
        c._client = TestClient(fake.app, base_url="https://www.acinfinityserver.com")
        made.append(c)
        return c

    clock = Clock()
    lines = []

    def run(*args, confirm=None, inputs=("",)):
        it = iter(inputs)
        return tool.main([*args, "--window", "30", "--poll", "10", "--write-gap", "5",
                          "--report-dir", str(tmp_path)] + (["--confirm", confirm] if confirm else []),
                         client_factory=factory, input_fn=lambda _: next(it), out=lines.append,
                         sleep=clock.sleep, clock=clock.now)
    return run, lines, made, clock, tmp_path


def writes():
    return len(fake.S.writes)


def live(port=1):
    return fake.S.port(DEV, port)


# ── refusals: nothing written ─────────────────────────────────────

def test_missing_allowlist_refuses_before_login(env, monkeypatch):
    run, lines, made, *_ = env
    monkeypatch.delenv("ACDASH_EXPERIMENT_DEV_ID")
    assert run("--port", "1", "--live", confirm="CI Flower Tent") == 2
    assert made == [] and writes() == 0
    assert "REFUSED" in lines[-1]


def test_other_controller_refused(env, monkeypatch):
    run, lines, *_ = env
    monkeypatch.setenv("ACDASH_EXPERIMENT_DEV_ID", "123456789")
    assert run("--port", "1", "--live", confirm="CI Flower Tent") == 2
    assert writes() == 0


def test_name_mismatch_refused(env, monkeypatch):
    run, lines, *_ = env
    monkeypatch.setenv("ACDASH_EXPERIMENT_DEV_ID", OTHER)   # real device, but it's the Veg tent
    monkeypatch.setenv("ACDASH_EXPERIMENT_DEV_NAME", "Flower")
    assert run("--port", "1", "--live", confirm="CI Veg Tent") == 2
    assert writes() == 0 and "does not contain" in lines[-1]


def test_empty_port_refused(env):
    run, lines, *_ = env
    assert run("--port", "4", "--live", confirm="CI Flower Tent") == 2
    assert writes() == 0 and "nothing is plugged in" in lines[-1]


def test_port_not_in_on_mode_refused(env):
    run, lines, *_ = env
    assert run("--port", "2", "--live", confirm="CI Flower Tent") == 2
    assert writes() == 0 and "not in On mode" in lines[-1]


def test_automation_port_refused(env):
    run, lines, *_ = env
    fake.S.modes[(DEV, 1)]["isOpenAutomation"] = 1
    assert run("--port", "1", "--live", confirm="CI Flower Tent") == 2
    assert writes() == 0 and "Advance Automation" in lines[-1]


def test_shared_controller_refused(env):
    run, lines, *_ = env
    fake.S.devices[0]["isShare"] = 1
    assert run("--port", "1", "--live", confirm="CI Flower Tent") == 2
    assert writes() == 0


def test_dry_run_writes_nothing_and_shows_plan(env):
    run, lines, *_ = env
    assert run("--port", "1") == 0
    assert writes() == 0
    text = "\n".join(lines)
    assert "DRY RUN" in text and "onlyUpdateSpeed=1" in text and "Backroads4Me" in text


def test_no_port_lists_ports(env):
    run, lines, *_ = env
    assert run() == 0 and writes() == 0
    assert any("port 4" in l and "(empty)" in l for l in lines)


def test_wrong_confirmation_refused(env):
    run, lines, *_ = env
    assert run("--port", "1", "--live", inputs=("Veg Tent",)) == 2
    assert writes() == 0


def test_write_before_verification_blocked():
    exp = tool.Experiment(object(), DEV, "Flower", 1)
    with pytest.raises(tool.Refused):
        exp._write({"mode": "off"})


# ── measurement + restore ────────────────────────────────────────

def _report(tmp_path):
    files = sorted(tmp_path.glob("*-control-experiment.json"))
    return json.loads(files[-1].read_text()), files[-1].read_text()


def test_finds_only_update_speed_and_restores(env):
    run, lines, made, clock, tmp_path = env
    fake.S.speed_only_needs = {"onlyUpdateSpeed": "1"}       # the #166 hypothesis
    assert run("--port", "1", "--live", confirm="CI Flower Tent") == 0
    rep, raw = _report(tmp_path)
    assert rep["reproduced_problem"] is True
    assert rep["applied_variants"][:2] == ["onlyUpdateSpeed=1", "onlyUpdateSpeed=1 (repeat)"]
    assert rep["restore_failed"] is False
    assert all(r["restore"].startswith("confirmed") for r in rep["results"])
    assert (live()["speak"], live()["curMode"]) == (5, 2)            # port back where it was
    for secret in ("ci-password", "ci@example.com", made[0].token):
        assert secret not in raw
    assert all(w["query"].get("devId", w["form"].get("devId")) == DEV for w in fake.S.writes)


def test_finds_app_headers(env):
    run, _, _, _, tmp_path = env
    fake.S.speed_only_needs = {"header:devtype": "11", "header:minversion": ""}
    assert run("--port", "1", "--live", confirm="CI Flower Tent") == 0
    rep, _ = _report(tmp_path)
    assert "app headers (devType, empty minversion)" in rep["applied_variants"]
    assert "onlyUpdateSpeed=1" not in rep["applied_variants"]
    assert (live()["speak"], live()["curMode"]) == (5, 2)


def test_combos_tried_when_no_single_knob_works(env):
    run, _, _, _, tmp_path = env
    fake.S.speed_only_needs = {"onlyUpdateSpeed": "1", "header:devtype": "11"}
    assert run("--port", "1", "--live", confirm="CI Flower Tent") == 0
    rep, _ = _report(tmp_path)
    assert "app 2.0.0 recipe" in rep["applied_variants"]
    assert (live()["speak"], live()["curMode"]) == (5, 2)


def test_everything_ignored_still_ends_at_original(env):
    run, _, _, _, tmp_path = env
    fake.S.ignore_writes = True
    assert run("--port", "1", "--live", confirm="CI Flower Tent") == 0
    rep, _ = _report(tmp_path)
    assert rep["applied_variants"] == [] and rep["reproduced_problem"] is False
    assert (live()["speak"], live()["curMode"]) == (5, 2)


def test_restore_bounces_mode_when_plain_restore_ignored(env):
    run, lines, _, _, tmp_path = env
    fake.S.speed_only_needs = None  # everything applies…
    assert run("--port", "1", "--live", "--only", "baseline (current default)", confirm="CI Flower Tent") == 0
    # …then make speed-only writes stick-but-ignore so the plain restore can't bring speed back
    fake.S.reset(); control._reset_rate_limit()
    exp_lines = []
    ck = Clock()
    exp = tool.Experiment(ACInfinityClient("ci@example.com", "ci-password"), DEV, "Flower", 1,
                          window=30, poll=10, write_gap=5, sleep=ck.sleep, clock=ck.now, out=exp_lines.append)
    exp.client._client.close()
    exp.client._client = TestClient(fake.app, base_url="https://www.acinfinityserver.com")
    exp.check_target(); exp.check_port()
    fake.S.port(DEV, 1)["speak"] = 7                                  # device drifted to 7
    fake.S.speed_only_needs = {"onlyUpdateSpeed": "1"}                 # plain speed restore ignored
    assert exp.restore() == "confirmed-after-bounce"
    assert (live()["speak"], live()["curMode"]) == (5, 2)


def test_ctrl_c_restores(env):
    run, lines, _, clock, tmp_path = env
    fake.S.speed_only_needs = None
    clock.interrupt_at = 3            # interrupt while watching the first variant
    rc = run("--port", "1", "--live", confirm="CI Flower Tent")
    assert rc in (0, 3)
    assert any("Interrupted" in l for l in lines)
    assert (live()["speak"], live()["curMode"]) == (5, 2)


def test_original_settings_saved_before_first_write(env):
    run, _, _, _, tmp_path = env
    assert run("--port", "1", "--live", "--only", "baseline (current default)", confirm="CI Flower Tent") == 0
    saved = list(tmp_path.glob("*-original-port1.json"))
    assert saved and json.loads(saved[0].read_text())["onSpead"] == 5


def test_restore_not_confirmed_is_loud(env):
    fake.S.reset(); control._reset_rate_limit()
    ck = Clock()
    lines = []
    exp = tool.Experiment(ACInfinityClient("ci@example.com", "ci-password"), DEV, "Flower", 1,
                          window=30, poll=10, write_gap=5, sleep=ck.sleep, clock=ck.now, out=lines.append)
    exp.client._client.close()
    exp.client._client = TestClient(fake.app, base_url="https://www.acinfinityserver.com")
    exp.check_target(); exp.check_port()
    fake.S.port(DEV, 1)["speak"] = 9      # device stuck at 9 …
    fake.S.ignore_writes = True           # … and ignores everything, including the mode bounce
    assert exp.restore() == "NOT CONFIRMED"
    assert exp.restore_failed is True
    rep = exp.report([])
    assert rep["restore_failed"] is True
