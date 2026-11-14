"""A slow port write must not freeze the server (it once ran blocking cloud I/O on the event loop)."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi.responses import JSONResponse


@pytest.mark.parametrize("path,body", [
    ("/api/port-control", {"dev_id": "9", "port": 1, "mode": "off"}),
    ("/api/port-restore", {"dev_id": "9", "port": 1}),
])
def test_slow_port_write_does_not_block_health(monkeypatch, path, body):
    monkeypatch.setenv("ACDASH_USE_ENV_CREDENTIALS", "1")
    monkeypatch.setenv("ACINFINITY_EMAIL", "user@example.com")
    monkeypatch.setenv("ACINFINITY_PASSWORD", "right")
    import app.main as main

    def slow_write(*a, **k):
        time.sleep(1.0)  # stands in for blocking cloud calls
        return JSONResponse({"ok": True})

    monkeypatch.setattr(main, "_write_and_verify", slow_write)
    monkeypatch.setattr(main.storage, "latest_settings_snapshot", lambda d, p: {"record": {}, "ts": 0})

    async def run():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://dash.local:8080") as c:
            t0 = time.monotonic()
            write = asyncio.create_task(c.post(path, json=body))
            await asyncio.sleep(0.2)
            health = await c.get("/health")
            health_at = time.monotonic() - t0
            write_was_running = not write.done()
            assert (await write).status_code == 200
            return health.status_code, health_at, write_was_running

    code, health_at, write_was_running = asyncio.run(run())
    assert code == 200
    # If the write blocked the event loop, /health could only answer after the 1 s write.
    assert write_was_running and health_at < 0.7, f"/health answered at {health_at:.2f}s, behind the write"
