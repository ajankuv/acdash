"""Fake AC Infinity cloud API for CI smoke tests.

Implements just enough of http://www.acinfinityserver.com/api for acdash to log in,
render the dashboard, read port settings, write port modes, and load history.
All data is synthetic (no real account, device IDs, or Wi-Fi names).

Test hooks (not part of the real API):
  GET  /__writes          → every addDevMode request received (transport, fields)
  POST /__reset           → restore initial state
  POST /__behavior        → JSON {ignore_writes, expire_next, rate_limit_next, write_code}
  GET  /__calls           → request counts per path (e.g. login count)
"""
from __future__ import annotations

import copy
import json
import time
from collections import Counter
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PASSWORD = "ci-password"
TOKEN = "fake-token-0001"

app = FastAPI(title="fake-acinfinity")


def _port(n: int, name: str, speed: int, *, resistance: int = 5000, at_type: int = 2) -> dict[str, Any]:
    return {
        "port": n,
        "portName": name,
        "speak": speed,
        "loadState": 1 if speed > 0 else 0,
        "online": 1,
        "curMode": at_type,
        "portResistance": resistance,
        "loadType": 1,
        "deviceType": 1,
        "overcurrentStatus": 0,
        "abnormalState": 0,
    }


def _initial_devices() -> list[dict[str, Any]]:
    return [
        {
            "devId": "900000000000000001",
            "devName": "CI Flower Tent",
            "devType": 11,
            "isShare": 0,
            "online": 1,
            "portCount": 4,
            "firmwareVersion": "3.2.56",
            "hardwareVersion": "1.0",
            "wifiName": "ci-network",
            "deviceInfo": {
                "temperature": 2450,
                "humidity": 5520,
                "vpdnums": 138,
                "tTrend": 0,
                "hTrend": 0,
                "curMode": 2,
                "ports": [
                    _port(1, "Exhaust Fan", 5),
                    _port(2, "Humidifier", 0, at_type=1),
                    _port(3, "Light", 10),
                    _port(4, "Empty", 0, resistance=65535, at_type=1),
                ],
                "sensors": [],
            },
        },
        {
            "devId": "900000000000000002",
            "devName": "CI Veg Tent",
            "devType": 11,
            "isShare": 0,
            "online": 1,
            "portCount": 4,
            "firmwareVersion": "3.2.56",
            "hardwareVersion": "1.0",
            "wifiName": "ci-network",
            "deviceInfo": {
                "temperature": 2300,
                "humidity": 6000,
                "vpdnums": 112,
                "tTrend": 1,
                "hTrend": 2,
                "curMode": 2,
                "ports": [
                    _port(1, "Inline Fan", 4),
                    _port(2, "Clip Fan", 3),
                    _port(3, "Heater", 0, at_type=1),
                    _port(4, "Dehumidifier", 0, at_type=1),
                ],
                "sensors": [],
            },
        },
    ]


def _mode_record(dev_id: str, port: dict[str, Any]) -> dict[str, Any]:
    """A getdevModeSettingList-style full settings record (subset of the ~142 real fields)."""
    return {
        "devId": dev_id,
        "externalPort": port["port"],
        "modeSetid": f"ms-{dev_id[-1]}-{port['port']}",
        "atType": port["curMode"],
        "modeType": 0,
        "onSpead": port["speak"] or 5,
        "offSpead": 0,
        "speak": port["speak"],
        "loadState": port["loadState"],
        "surplus": 0,
        "acitveTimerOn": 0,
        "acitveTimerOff": 0,
        "activeCycleOn": 60,
        "activeCycleOff": 60,
        "schedStartTime": 65535,
        "schedEndtTime": 65535,
        "targetVpd": 120,
        "targetVpdSwitch": 0,
        "activeHt": 0, "activeLt": 0, "activeHh": 0, "activeLh": 0,
        "devHt": 30, "devLt": 18, "devHtf": 86, "devLtf": 64,
        "devHh": 70, "devLh": 40,
        "vpdHighEnable": 0, "vpdLowEnable": 0, "vpdHighTrigger": 150, "vpdLowTrigger": 80,
        "isOpenAutomation": 0,
        "settingMode": 0,
        "devMacAddr": "",
        "ecoMode": False,
        "devSetting": {"devId": dev_id, "calibrationTime": 0, "tempUnit": 0},
    }


class State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.devices = _initial_devices()
        self.modes: dict[tuple[str, int], dict[str, Any]] = {}
        for d in self.devices:
            for p in d["deviceInfo"]["ports"]:
                self.modes[(d["devId"], p["port"])] = _mode_record(d["devId"], p)
        self.writes: list[dict[str, Any]] = []
        self.calls: Counter[str] = Counter()
        self.ignore_writes = False  # cloud stores the value but the "device" never applies it
        self.expire_next = 0        # next N token requests answer code 10003
        self.rate_limit_next = 0    # next N log requests answer code 999998
        self.write_code = 200       # force addDevMode result code

    def port(self, dev_id: str, n: int) -> dict[str, Any] | None:
        for d in self.devices:
            if d["devId"] == dev_id:
                for p in d["deviceInfo"]["ports"]:
                    if p["port"] == n:
                        return p
        return None


S = State()


async def _params(request: Request) -> tuple[dict[str, str], dict[str, str]]:
    query = dict(request.query_params)
    form: dict[str, str] = {}
    ctype = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
        form = {k: str(v) for k, v in (await request.form()).items()}
    return query, form


def _ok(data: Any) -> JSONResponse:
    return JSONResponse({"code": 200, "msg": "success", "data": data})


def _token_problem(request: Request) -> JSONResponse | None:
    if request.headers.get("token") != TOKEN:
        return JSONResponse({"code": 10003, "msg": "Login Expired", "data": None})
    if S.expire_next > 0:
        S.expire_next -= 1
        return JSONResponse({"code": 10003, "msg": "Login Expired", "data": None})
    return None


@app.middleware("http")
async def count_calls(request: Request, call_next):
    S.calls[request.url.path] += 1
    return await call_next(request)


@app.post("/api/user/appUserLogin")
async def login(request: Request) -> JSONResponse:
    q, f = await _params(request)
    fields = {**q, **f}
    if fields.get("appPasswordl") != PASSWORD or not fields.get("appEmail"):
        return JSONResponse({"code": 10001, "msg": "Incorrect Password", "data": None})
    return _ok({"appId": TOKEN, "nickName": "ci", "secretId": "ci-secret", "requestApp": "ci-app"})


@app.post("/api/user/devInfoListAll")
async def dev_info_list_all(request: Request) -> JSONResponse:
    if (p := _token_problem(request)) is not None:
        return p
    return _ok(copy.deepcopy(S.devices))


@app.post("/api/dev/getdevModeSettingList")
async def get_mode(request: Request) -> JSONResponse:
    if (p := _token_problem(request)) is not None:
        return p
    q, f = await _params(request)
    fields = {**q, **f}
    rec = S.modes.get((str(fields.get("devId")), int(fields.get("port") or 0)))
    if rec is None:
        return JSONResponse({"code": 999999, "msg": "Operation failed, please try again", "data": None})
    return _ok(copy.deepcopy(rec))


@app.post("/api/dev/getDevSetting")
async def get_dev_setting(request: Request) -> JSONResponse:
    if (p := _token_problem(request)) is not None:
        return p
    return _ok({"tempUnit": 0, "calibrationTime": 0})


def _coerce(v: str) -> Any:
    try:
        return int(v)
    except ValueError:
        return v


@app.post("/api/dev/addDevMode")
async def add_dev_mode(request: Request) -> JSONResponse:
    if (p := _token_problem(request)) is not None:
        return p
    q, f = await _params(request)
    fields = {**q, **f}
    transport = "form" if f else "query"
    S.writes.append(
        {
            "transport": transport,
            "content_type": request.headers.get("content-type", ""),
            "query": q,
            "form": f,
            "headers": {k: v for k, v in request.headers.items() if k.lower() in ("sign", "requestapp", "requestid", "version", "minversion", "devtype", "user-agent")},
            "ts": time.time(),
        }
    )
    dev_id = str(fields.get("devId"))
    port_n = int(fields.get("externalPort") or 0)
    port = S.port(dev_id, port_n)
    if port is None:
        return JSONResponse({"code": 999999, "msg": "Operation failed, please try again", "data": None})
    if port["portResistance"] == 65535:
        return JSONResponse({"code": 999999, "msg": "Operation failed, please try again", "data": None})
    if S.write_code != 200:
        return JSONResponse({"code": S.write_code, "msg": "Forced failure", "data": None})
    rec = S.modes[(dev_id, port_n)]
    for k, v in fields.items():
        if k in rec and k != "devSetting":
            rec[k] = _coerce(v)
    if not S.ignore_writes:
        at = int(rec.get("atType", port["curMode"]))
        port["curMode"] = at
        if at == 1:
            port["speak"] = int(rec.get("offSpead") or 0)
        elif at == 2:
            port["speak"] = int(rec.get("onSpead") or 0)
        port["loadState"] = 1 if port["speak"] else 0
    return _ok(None)


@app.post("/api/log/dataPage")
async def data_page(request: Request) -> JSONResponse:
    if (p := _token_problem(request)) is not None:
        return p
    if S.rate_limit_next > 0:
        S.rate_limit_next -= 1
        return JSONResponse({"code": 999998, "msg": "Rate Limiting!", "data": None})
    q, f = await _params(request)
    fields = {**q, **f}
    newer = int(fields.get("time") or time.time())
    older = int(fields.get("endTime") or newer - 3600)
    size = min(int(fields.get("pageSize") or 1000), 2000)
    rows = []
    t = newer - (newer % 60)
    while t >= older and len(rows) < size:
        rows.append(
            {
                "createTime": t,
                "temperature": 2400 + (t // 60) % 50,
                "humidity": 5500 + (t // 60) % 100,
                "vpdNums": 130,
                "allSpead": 5,
                "portSpead": 0x5050,
                "portStatus": 0b0101,
            }
        )
        t -= 60
    return _ok({"rows": rows, "total": len(rows), "validFrom": older})


@app.post("/api/log/logdataByAll")
async def log_data_by_all(request: Request) -> JSONResponse:
    if (p := _token_problem(request)) is not None:
        return p
    now = int(time.time())
    rows = [
        {"id": 3, "createTime": now - 60, "logType": 4, "businessType": 0, "portSelection": 1, "currentMode": 2},
        {"id": 2, "createTime": now - 600, "logType": 2, "businessType": 1, "portSelection": 0, "alarmHighTemp": 1},
        {"id": 1, "createTime": now - 900, "logType": 3, "businessType": 1, "portSelection": 2,
         "mlVariationTrend": 2, "pauseReason": 4, "currentStatus": 1},
    ]
    return _ok({"rows": rows, "total": len(rows), "validFrom": now - 86400})


@app.post("/api/version=2.0/dev/getGroups")
async def get_groups(request: Request) -> JSONResponse:
    if (p := _token_problem(request)) is not None:
        return p
    return _ok([])


@app.get("/__writes")
async def writes() -> JSONResponse:
    return JSONResponse(S.writes)


@app.get("/__calls")
async def calls() -> JSONResponse:
    return JSONResponse(dict(S.calls))


@app.post("/__reset")
async def reset() -> JSONResponse:
    S.reset()
    return JSONResponse({"ok": True})


@app.post("/__behavior")
async def behavior(request: Request) -> JSONResponse:
    body = json.loads(await request.body() or b"{}")
    for key in ("ignore_writes", "expire_next", "rate_limit_next", "write_code"):
        if key in body:
            setattr(S, key, body[key])
    return JSONResponse({"ok": True})


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})
