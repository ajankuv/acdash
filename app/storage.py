from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("HISTORY_DB_PATH", "/app/data/history.db")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS readings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dev_id       TEXT    NOT NULL,
    ts           INTEGER NOT NULL,
    temp_c       REAL,
    humidity_pct REAL,
    vpd_kpa      REAL,
    fan          INTEGER,
    sensors_json TEXT
);
"""

_CREATE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_dev_ts ON readings (dev_id, ts);
"""

_CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS controller_meta (
    dev_id TEXT PRIMARY KEY,
    stage  TEXT NOT NULL
);
"""


_CREATE_SNAPSHOT_TABLE = """
CREATE TABLE IF NOT EXISTS settings_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dev_id      TEXT    NOT NULL,
    port        INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    record_json TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'write'
);
"""

_CREATE_SNAPSHOT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_snap_dev_port ON settings_snapshots (dev_id, port, id);
"""

SNAPSHOTS_KEPT_PER_PORT = 20


def _migrate_reading_source(conn: sqlite3.Connection) -> None:
    """Add ``readings.source`` ('local' collector | 'cloud' backfill) if missing. Idempotent;
    existing rows become 'local'. Older images keep working: every statement names its columns."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(readings)").fetchall()}
    if "source" not in cols:
        conn.execute("ALTER TABLE readings ADD COLUMN source TEXT NOT NULL DEFAULT 'local'")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        with _connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_INDEX)
            conn.execute(_CREATE_META_TABLE)
            conn.execute(_CREATE_SNAPSHOT_TABLE)
            conn.execute(_CREATE_SNAPSHOT_INDEX)
            _migrate_reading_source(conn)
        logger.info("history db ready: %s", DB_PATH)
    except Exception:
        logger.exception("failed to init history db at %s", DB_PATH)


def insert_reading(
    dev_id: str,
    ts: int,
    temp_c: float | None,
    humidity_pct: float | None,
    vpd_kpa: float | None,
    fan: int | None,
    sensors: list[dict[str, Any]],
) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO readings
                    (dev_id, ts, temp_c, humidity_pct, vpd_kpa, fan, sensors_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (dev_id, ts, temp_c, humidity_pct, vpd_kpa, fan, json.dumps(sensors)),
            )
    except Exception:
        logger.exception("insert_reading failed for dev_id=%s ts=%s", dev_id, ts)


def get_all_stages() -> dict[str, str]:
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT dev_id, stage FROM controller_meta").fetchall()
        return {row["dev_id"]: row["stage"] for row in rows}
    except Exception:
        logger.exception("get_all_stages failed")
        return {}


def set_controller_stage(dev_id: str, stage: str) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO controller_meta (dev_id, stage) VALUES (?, ?)",
                (dev_id, stage),
            )
    except Exception:
        logger.exception("set_controller_stage failed for dev_id=%s", dev_id)


def count_readings(dev_id: str, start_ts: int, end_ts: int) -> int:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM readings WHERE dev_id=? AND ts>=? AND ts<=?",
                (dev_id, start_ts, end_ts),
            ).fetchone()
            return row[0] if row else 0
    except Exception:
        logger.exception("count_readings failed")
        return 0


def query_readings(dev_id: str, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, temp_c, humidity_pct, vpd_kpa, fan, source
                FROM readings
                WHERE dev_id=? AND ts>=? AND ts<=?
                ORDER BY ts ASC
                """,
                (dev_id, start_ts, end_ts),
            ).fetchall()
        return [
            {
                "t": row["ts"],
                "t_ms": row["ts"] * 1000,
                "temp_c": row["temp_c"],
                "rh": row["humidity_pct"],
                "vpd_kpa": row["vpd_kpa"],
                "fan": row["fan"],
                "port_fan": None,
                "source": row["source"],
            }
            for row in rows
        ]
    except Exception:
        logger.exception("query_readings failed")
        return []


def save_settings_snapshot(dev_id: str, port: int, record: dict[str, Any], source: str = "write") -> None:
    """Store a port's full pre-write settings record; keep the newest SNAPSHOTS_KEPT_PER_PORT."""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO settings_snapshots (dev_id, port, ts, record_json, source) VALUES (?, ?, ?, ?, ?)",
                (dev_id, int(port), int(time.time()), json.dumps(record), source),
            )
            conn.execute(
                """
                DELETE FROM settings_snapshots
                WHERE dev_id=? AND port=? AND id NOT IN (
                    SELECT id FROM settings_snapshots WHERE dev_id=? AND port=?
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (dev_id, int(port), dev_id, int(port), SNAPSHOTS_KEPT_PER_PORT),
            )
    except Exception:
        logger.exception("save_settings_snapshot failed for dev_id=%s port=%s", dev_id, port)


def latest_settings_snapshot(dev_id: str, port: int) -> dict[str, Any] | None:
    """Newest stored snapshot for a port: ``{id, ts, record, source}`` or None."""
    try:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT id, ts, record_json, source FROM settings_snapshots
                WHERE dev_id=? AND port=? ORDER BY id DESC LIMIT 1
                """,
                (dev_id, int(port)),
            ).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "ts": row["ts"], "record": json.loads(row["record_json"]), "source": row["source"]}
    except Exception:
        logger.exception("latest_settings_snapshot failed for dev_id=%s port=%s", dev_id, port)
        return None


def count_settings_snapshots(dev_id: str, port: int) -> int:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM settings_snapshots WHERE dev_id=? AND port=?", (dev_id, int(port))
            ).fetchone()
        return row[0] if row else 0
    except Exception:
        logger.exception("count_settings_snapshots failed")
        return 0


def find_gaps(dev_id: str, since_ts: int, until_ts: int, min_gap: int = 300) -> list[tuple[int, int]]:
    """Periods longer than ``min_gap`` seconds with no reading, within [since_ts, until_ts].

    Includes a leading gap (window start → first reading, e.g. a new install) and a trailing
    gap (last reading → until_ts, e.g. container was down until just now).
    """
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT prev_ts, ts FROM (
                    SELECT ts, LAG(ts) OVER (ORDER BY ts) AS prev_ts
                    FROM readings WHERE dev_id=? AND ts>=? AND ts<=?
                ) WHERE prev_ts IS NOT NULL AND ts - prev_ts > ?
                ORDER BY ts
                """,
                (dev_id, since_ts, until_ts, min_gap),
            ).fetchall()
            bounds = conn.execute(
                "SELECT MIN(ts), MAX(ts) FROM readings WHERE dev_id=? AND ts>=? AND ts<=?",
                (dev_id, since_ts, until_ts),
            ).fetchone()
    except Exception:
        logger.exception("find_gaps failed for dev_id=%s", dev_id)
        return []
    first, last = (bounds[0], bounds[1]) if bounds else (None, None)
    if first is None:
        return [(since_ts, until_ts)] if until_ts - since_ts > min_gap else []
    gaps: list[tuple[int, int]] = []
    if first - since_ts > min_gap:
        gaps.append((since_ts, first))
    gaps.extend((int(r[0]), int(r[1])) for r in rows)
    if until_ts - last > min_gap:
        gaps.append((last, until_ts))
    return gaps


def insert_cloud_readings(dev_id: str, points: list[dict[str, Any]], *, dedupe_secs: int = 30) -> int:
    """Insert backfilled cloud points (source='cloud'); never touches existing rows.

    Points within ``dedupe_secs`` of any existing reading are skipped (cloud and local clocks
    don't line up exactly). Returns rows inserted.
    """
    pts = sorted((p for p in points if p.get("t") is not None), key=lambda p: p["t"])
    if not pts:
        return 0
    lo, hi = int(pts[0]["t"]) - dedupe_secs, int(pts[-1]["t"]) + dedupe_secs
    inserted = 0
    try:
        with _connect() as conn:
            existing = [r[0] for r in conn.execute(
                "SELECT ts FROM readings WHERE dev_id=? AND ts>=? AND ts<=? ORDER BY ts", (dev_id, lo, hi)
            ).fetchall()]
            import bisect

            for p in pts:
                t = int(p["t"])
                i = bisect.bisect_left(existing, t - dedupe_secs)
                if i < len(existing) and existing[i] <= t + dedupe_secs:
                    continue
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO readings
                        (dev_id, ts, temp_c, humidity_pct, vpd_kpa, fan, sensors_json, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'cloud')
                    """,
                    (dev_id, t, p.get("temp_c"), p.get("rh"), p.get("vpd_kpa"), p.get("fan"), "[]"),
                )
                if cur.rowcount:
                    inserted += 1
                    bisect.insort(existing, t)
    except Exception:
        logger.exception("insert_cloud_readings failed for dev_id=%s", dev_id)
    return inserted


def oldest_reading_ts(dev_id: str | None = None) -> int | None:
    try:
        with _connect() as conn:
            if dev_id is None:
                row = conn.execute("SELECT MIN(ts) FROM readings").fetchone()
            else:
                row = conn.execute("SELECT MIN(ts) FROM readings WHERE dev_id=?", (dev_id,)).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except Exception:
        logger.exception("oldest_reading_ts failed")
        return None
