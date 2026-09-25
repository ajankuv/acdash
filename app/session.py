"""One shared, long-lived AC Infinity client per saved credential set.

Every route used to build a fresh ``ACInfinityClient`` and log in again (every 45 s snapshot,
every chart, every write). Now routes and the collector share one client, which keeps its
session token and renews it only when the API says it expired.
"""
from __future__ import annotations

import hashlib
import threading

from app.client import ACInfinityClient

_lock = threading.Lock()
_client: ACInfinityClient | None = None
_key: tuple[str, str] | None = None


def _cred_key(email: str, password: str) -> tuple[str, str]:
    return email, hashlib.sha256(password.encode("utf-8")).hexdigest()


# Other threads (requests, collector, backfill, verifier) may still be mid-request on a
# replaced client; closing it immediately makes those calls raise. Close it a bit later.
RETIRE_DELAY_SECS = 120.0


def _retire(old: ACInfinityClient | None) -> None:
    if old is None:
        return
    t = threading.Timer(RETIRE_DELAY_SECS, old.close)
    t.daemon = True
    t.start()


def get_client(email: str, password: str) -> ACInfinityClient:
    """Shared client for these credentials; replaced (old one closed) if credentials change."""
    global _client, _key
    key = _cred_key(email, password)
    with _lock:
        if _client is None or _key != key:
            old = _client
            _client = ACInfinityClient(email, password)
            _key = key
            _retire(old)
        return _client


def reset_client() -> None:
    """Drop the shared client (e.g. new credentials saved in the setup wizard)."""
    global _client, _key
    with _lock:
        old, _client, _key = _client, None, None
    _retire(old)
