"""Collect a large JSON bundle from AC Infinity for debugging / support pastebacks."""

from __future__ import annotations

import time
from typing import Any

from app.client import ACInfinityClient
from app.normalize import normalize_devices


def collect_debug_bundle(client: ACInfinityClient, timeout_secs: float = 90.0) -> dict[str, Any]:
    """Fetch devInfoListAll (full body) plus per-port getDevSetting and getdevModeSettingList."""
    deadline = time.monotonic() + timeout_secs
    bundle: dict[str, Any] = {
        "acdash": {
            "bundle": "ac-infinity-debug-v1",
            "collected_at_unix": time.time(),
        },
        "devInfoListAll": None,
        "normalized_controllers": [],
        "devices_enriched": [],
        "collection_errors": [],
    }

    full = client.get_dev_info_list_all_full()
    bundle["devInfoListAll"] = full

    if not full or not isinstance(full, dict):
        bundle["collection_errors"].append({"where": "devInfoListAll", "detail": "empty or non-object response"})
        return bundle

    if full.get("code") != 200:
        bundle["collection_errors"].append(
            {"where": "devInfoListAll", "code": full.get("code"), "msg": full.get("msg")}
        )
        return bundle

    raw_list = full.get("data")
    if not isinstance(raw_list, list):
        bundle["collection_errors"].append({"where": "devInfoListAll", "detail": "data is not a list"})
        return bundle

    try:
        bundle["normalized_controllers"] = normalize_devices(raw_list)
    except Exception as e:  # noqa: BLE001 — debug helper; keep partial bundle
        bundle["collection_errors"].append({"where": "normalize_devices", "detail": repr(e)})

    for device in raw_list:
        if not isinstance(device, dict):
            continue
        dev_id = device.get("devId")
        if dev_id is None:
            continue

        port_nums: set[int] = {0}
        info = device.get("deviceInfo")
        if isinstance(info, dict):
            for p in info.get("ports") or []:
                if not isinstance(p, dict):
                    continue
                raw_port = p.get("port")
                try:
                    port_nums.add(int(raw_port))
                except (TypeError, ValueError):
                    pass

        ports_out: dict[str, Any] = {}
        for port in sorted(port_nums):
            if time.monotonic() > deadline:
                bundle["collection_errors"].append(
                    {"where": "timeout", "detail": f"bundle collection exceeded {timeout_secs}s limit"}
                )
                return bundle
            pk = str(port)
            mode_body = client.get_dev_mode_setting_list(dev_id, port)
            setting_body = client.get_dev_setting(dev_id, port)
            ports_out[pk] = {
                "getdevModeSettingList": mode_body,
                "getDevSetting": setting_body,
            }
            for label, body in (("getdevModeSettingList", mode_body), ("getDevSetting", setting_body)):
                if body is None:
                    bundle["collection_errors"].append(
                        {"devId": dev_id, "port": port, "endpoint": label, "detail": "request failed or empty"}
                    )
                elif body.get("code") != 200:
                    bundle["collection_errors"].append(
                        {
                            "devId": dev_id,
                            "port": port,
                            "endpoint": label,
                            "code": body.get("code"),
                            "msg": body.get("msg"),
                        }
                    )

        bundle["devices_enriched"].append(
            {
                "devId": dev_id,
                "extra_api_calls_by_port": ports_out,
            }
        )

    return bundle
