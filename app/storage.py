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


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        with _connect() as conn:
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_INDEX)
            conn.execute(_CREATE_META_TABLE)
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
                SELECT ts, temp_c, humidity_pct, vpd_kpa, fan
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
            }
            for row in rows
        ]
    except Exception:
        logger.exception("query_readings failed")
        return []
