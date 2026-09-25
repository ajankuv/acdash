"""Every test gets its own throwaway SQLite DB (writes now store settings snapshots)."""
import pytest

from app import storage


@pytest.fixture(autouse=True)
def _tmp_history_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "history.db"))
    storage.init_db()
    yield
