"""add-cloud-history-backfill: migration, gap detection, dedupe, worker, config, app safety."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app import backfill, storage

NOW = 1_800_000_000
DAY = 86_400


def local(dev, *ts):
    for t in ts:
        storage.insert_reading(dev, t, 20.0, 50.0, 1.0, 5, [])


class CloudClient:
    """history_data_page returning 1-minute rows for the requested window."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.last_request_error = None

    def history_data_page(self, dev_id, time_end, time_start, *, page_size=1000, order_direction=1):
        self.calls.append((dev_id, time_start, time_end, page_size))
        if self.fail:
            raise RuntimeError("cloud down")
        rows = [{"createTime": t, "temperature": 2500, "humidity": 6000, "vpdNums": 120, "allSpead": 3}
                for t in range(time_end - time_end % 60, time_start - 1, -60)]
        return {"rows": rows[:page_size], "total": len(rows)}


# ── 1. Storage ─────────────────────────────────────────────────────


def test_source_migration_idempotent_and_preserves_rows(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY AUTOINCREMENT, dev_id TEXT NOT NULL,"
                 " ts INTEGER NOT NULL, temp_c REAL, humidity_pct REAL, vpd_kpa REAL, fan INTEGER, sensors_json TEXT)")
    conn.execute("CREATE UNIQUE INDEX idx_dev_ts ON readings (dev_id, ts)")
    conn.executemany("INSERT INTO readings (dev_id, ts, temp_c) VALUES ('1', ?, 21.0)", [(100,), (160,)])
    conn.commit()
    conn.close()
    monkeypatch.setattr(storage, "DB_PATH", str(db))
    storage.init_db()
    storage.init_db()
    pts = storage.query_readings("1", 0, 1000)
    assert [p["t"] for p in pts] == [100, 160]
    assert all(p["source"] == "local" for p in pts)


def test_old_image_statements_still_work_on_migrated_db():
    # The pre-change INSERT (no source column named) must still succeed → default 'local'.
    with storage._connect() as conn:
        conn.execute("INSERT OR IGNORE INTO readings (dev_id, ts, temp_c, humidity_pct, vpd_kpa, fan, sensors_json)"
                     " VALUES ('1', 5, 1, 1, 1, 1, '[]')")
        row = conn.execute("SELECT ts, temp_c, humidity_pct, vpd_kpa, fan FROM readings").fetchone()
    assert row[0] == 5
    assert storage.query_readings("1", 0, 10)[0]["source"] == "local"


def test_gaps_continuous_none():
    local("1", *range(NOW - 3600, NOW, 60))
    assert storage.find_gaps("1", NOW - 3600, NOW - 60) == []


def test_gaps_single_hole():
    local("1", *range(NOW - 7200, NOW - 5400, 60), *range(NOW - 1800, NOW, 60))
    gaps = storage.find_gaps("1", NOW - 7200, NOW - 60)
    assert gaps == [(NOW - 5460, NOW - 1800)]


def test_gaps_new_install_is_whole_window():
    assert storage.find_gaps("1", NOW - DAY, NOW) == [(NOW - DAY, NOW)]


def test_gaps_leading_and_trailing():
    local("1", NOW - 5000, NOW - 4940)
    gaps = storage.find_gaps("1", NOW - 10_000, NOW)
    assert gaps[0] == (NOW - 10_000, NOW - 5000)
    assert gaps[-1] == (NOW - 4940, NOW)


def test_cloud_insert_never_overwrites_and_dedupes():
    local("1", NOW - 600)
    before = storage.query_readings("1", 0, NOW)[0]
    n = storage.insert_cloud_readings("1", [
        {"t": NOW - 600, "temp_c": 99.0},   # same ts → skipped
        {"t": NOW - 590, "temp_c": 99.0},   # within 30 s → skipped
        {"t": NOW - 540, "temp_c": 25.0},   # new
        {"t": NOW - 480, "temp_c": 25.0},   # new
    ])
    assert n == 2
    rows = storage.query_readings("1", 0, NOW)
    assert rows[0] == {**before}
    assert [r["source"] for r in rows] == ["local", "cloud", "cloud"]


# ── 2. Worker ──────────────────────────────────────────────────────


def test_backfill_fills_gap_one_request_per_day(monkeypatch):
    monkeypatch.setenv("BACKFILL_DAYS", "2")
    # continuous local data except a 2 h hole yesterday
    hole = (NOW - DAY - 7200, NOW - DAY)
    ts = [t for t in range(NOW - 2 * DAY, NOW - 60, 60) if not (hole[0] < t < hole[1])]
    local("1", *ts)
    c = CloudClient()
    rows, reqs = backfill.backfill_controller(c, "1", now=NOW)
    assert reqs == 1
    assert 110 <= rows <= 120
    assert c.calls[0][1] == hole[0] - 60 or c.calls[0][1] <= hole[0]
    assert storage.find_gaps("1", NOW - 2 * DAY, NOW - backfill.RECENT_MARGIN_SECS) == []


def test_new_install_chunks_by_day(monkeypatch):
    monkeypatch.setenv("BACKFILL_DAYS", "3")
    c = CloudClient()
    backfill.backfill_controller(c, "1", now=NOW)
    assert len(c.calls) == 3
    assert all(end - start <= backfill.CHUNK_SECS for _, start, end, _ in c.calls)


def test_no_gaps_no_requests(monkeypatch):
    monkeypatch.setenv("BACKFILL_DAYS", "1")
    local("1", *range(NOW - DAY - 60, NOW, 60))
    c = CloudClient()
    assert backfill.backfill_controller(c, "1", now=NOW) == (0, 0)
    assert c.calls == []


@pytest.mark.parametrize("raw,expected", [(None, 30), ("0", 0), ("7", 7), ("500", 90), ("-3", 0), ("x", 30)])
def test_backfill_days(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("BACKFILL_DAYS", raising=False)
    else:
        monkeypatch.setenv("BACKFILL_DAYS", raw)
    assert backfill.backfill_days() == expected


def test_disabled_makes_no_requests(monkeypatch):
    monkeypatch.setenv("BACKFILL_DAYS", "0")
    c = CloudClient()
    assert backfill.run_once(c, ["1"], now=NOW) == 0
    assert c.calls == []

    async def go():
        await backfill.backfill_loop(lambda: c, lambda: ["1"])
    asyncio.run(go())  # returns immediately when disabled
    assert c.calls == []


def test_failure_recorded_not_raised(monkeypatch):
    monkeypatch.setenv("BACKFILL_DAYS", "1")
    c = CloudClient(fail=True)
    assert backfill.run_once(c, ["1"], now=NOW) == 0
    assert "cloud down" in backfill.status["last_error"]
    assert backfill.status["running"] is False


def test_loop_survives_errors_and_repeats(monkeypatch):
    monkeypatch.setenv("BACKFILL_DAYS", "1")
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    def bad_client():
        raise RuntimeError("no creds yet")

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await backfill.backfill_loop(bad_client, lambda: ["1"], sleep=fake_sleep)
    asyncio.run(go())
    assert sleeps == [backfill.STARTUP_DELAY_SECS, backfill.INTERVAL_SECS, backfill.INTERVAL_SECS]


# ── 3. App integration ─────────────────────────────────────────────


def test_app_serves_while_backfill_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKFILL_DAYS", "1")
    monkeypatch.setattr(backfill, "STARTUP_DELAY_SECS", 0)
    import app.main as main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "_get_client_for_backfill", lambda: CloudClient(fail=True))
    monkeypatch.setattr(main, "_get_dev_ids_for_backfill", lambda: ["1"])
    monkeypatch.setattr(main, "ENV_FILE_PATH", tmp_path / ".env")
    monkeypatch.delenv("ACDASH_USE_ENV_CREDENTIALS", raising=False)
    with TestClient(main.app) as tc:  # runs lifespan (collector + backfill)
        assert tc.get("/health").status_code == 200
        assert tc.get("/", follow_redirects=False).status_code in (200, 302)


def test_debug_status_has_oldest_ts():
    local("1", NOW - 100)
    assert backfill.debug_status()["oldest_local_ts"] == NOW - 100


def test_chart_meta_counts_backfilled(monkeypatch):
    import time as _t
    now = int(_t.time())
    local("1", *range(now - 3600, now - 1800, 60))
    storage.insert_cloud_readings("1", [{"t": t, "temp_c": 20.0} for t in range(now - 1800 + 60, now, 60)])
    monkeypatch.setenv("ACDASH_USE_ENV_CREDENTIALS", "1")
    monkeypatch.setenv("ACINFINITY_EMAIL", "a@b.c")
    monkeypatch.setenv("ACINFINITY_PASSWORD", "x")
    import app.main as main
    from fastapi.testclient import TestClient

    main._history_cache.clear()
    body = TestClient(main.app).get("/api/history-chart?dev_id=1&hours=1").json()
    assert body["meta"]["source"] == "local"
    assert body["meta"]["backfilled_points"] > 20


def test_backfill_helpers_are_plain_functions(monkeypatch):
    """Regression: a misplaced @asynccontextmanager once wrapped _get_client_for_backfill."""
    monkeypatch.setenv("ACDASH_USE_ENV_CREDENTIALS", "1")
    monkeypatch.setenv("ACINFINITY_EMAIL", "a@b.c")
    monkeypatch.setenv("ACINFINITY_PASSWORD", "x")
    import app.main as main
    from app.client import ACInfinityClient

    client = main._get_client_for_backfill()
    assert isinstance(client, ACInfinityClient)
    assert hasattr(client, "history_data_page")
    assert hasattr(main.lifespan(main.app), "__aenter__")  # lifespan itself is the context manager
