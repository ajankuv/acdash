import asyncio
import json
import logging
import time
from typing import Any, Callable

from app import storage

logger = logging.getLogger(__name__)

COLLECTOR_INTERVAL = int(__import__("os").getenv("COLLECTOR_INTERVAL_SECONDS", "60"))


async def collector_loop(get_controllers_fn: Callable[[], list[dict[str, Any]]]) -> None:
    logger.info("collector started (interval=%ds, db=%s)", COLLECTOR_INTERVAL, storage.DB_PATH)
    while True:
        await asyncio.sleep(COLLECTOR_INTERVAL)
        try:
            controllers: list[dict[str, Any]] = await asyncio.to_thread(get_controllers_fn)
            if not controllers:
                continue
            ts = int(time.time())
            for c in controllers:
                dev_id = c.get("id")
                if not dev_id:
                    continue
                fan: int | None = None
                for p in c.get("ports", []):
                    spd = p.get("speed")
                    if spd is not None:
                        fan = int(spd)
                        break
                sensors = [
                    {"type": s.get("type"), "value": s.get("value"), "suffix": s.get("suffix")}
                    for s in c.get("sensors", [])
                    if s.get("value") is not None
                ]
                await asyncio.to_thread(
                    storage.insert_reading,
                    dev_id,
                    ts,
                    c.get("temp_c"),
                    c.get("humidity_pct"),
                    c.get("vpd_kpa"),
                    fan,
                    sensors,
                )
            logger.debug("collector stored %d controller(s) at ts=%d", len(controllers), ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("collector error")
