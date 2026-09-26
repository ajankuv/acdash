"""WriteVariant knobs (tools/control_experiment.py) — and proof the default path is unchanged."""
from __future__ import annotations

import pytest

import app.control as control
from app.control import WriteVariant, apply_variant, build_write_payload, write_port_control
from tests.fakes import FakeAPI, make_client


def record(**over):
    rec = {"devId": "1", "externalPort": 1, "modeSetid": "ms", "atType": 2, "modeType": 0, "onSpead": 5,
           "offSpead": 0, "onlyUpdateSpeed": 0, "devHtf": 86, "devLtf": 0, "targetTempF": 0,
           "waterTempLowValueF": 12, "devSetting": {"a": 1}, "ecoMode": False}
    rec.update(over)
    return rec


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("ACINFINITY_WRITE_FORMAT", "ACINFINITY_SIGN_WRITES"):
        monkeypatch.delenv(k, raising=False)
    control._reset_rate_limit()
    yield
    control._reset_rate_limit()


def send(variant=None, rec=None):
    control._reset_rate_limit()
    api = FakeAPI()
    api.queue("/dev/getdevModeSettingList", (200, {"code": 200, "data": rec or record()}))
    api.queue("/user/devInfoListAll", (200, {"code": 200, "data": [{"devId": "1", "devType": 11}]}))
    c = make_client(api)
    c.get_devices()
    write_port_control(c, "1", 1, {"mode": "manual", "state": True, "speed": 7}, variant=variant)
    path, query, form, headers = [x for x in api.calls if x[0].endswith("/dev/addDevMode")][-1]
    return path, query, form, headers


def test_default_request_is_exactly_the_original_recipe():
    path, query, form, headers = send()
    expected = build_write_payload(record(), control.build_mode_payload("1", 1, control.normalize_port_settings([record()]),
                                   {"mode": "manual", "state": True, "speed": 7}))
    expected = {k: str(v) for k, v in {**expected, "devId": "1", "externalPort": 1}.items()}
    assert query == expected
    assert form == {}
    assert not {"sign", "devtype", "minversion", "requestapp"} & set(headers)
    assert path.startswith("/api/dev/addDevMode")


def test_empty_variant_changes_nothing():
    assert send() [1:3] == send(WriteVariant(name="noop"))[1:3]
    assert apply_variant({"a": 1}, None) == {"a": 1}


def test_only_update_speed():
    assert send(WriteVariant(only_update_speed=1))[1]["onlyUpdateSpeed"] == "1"


def test_clamp_f_raises_below_32_only():
    q = send(WriteVariant(clamp_f=True))[1]
    assert (q["devLtf"], q["targetTempF"], q["waterTempLowValueF"], q["devHtf"]) == ("32", "32", "32", "86")
    q0 = send()[1]
    assert q0["devLtf"] == "0"


def test_force_mode_type():
    assert send(WriteVariant(force_mode_type=2))[1]["modeType"] == "2"


def test_form_override():
    _, query, form, _ = send(WriteVariant(fmt="form"))
    assert query == {} and form["onSpead"] == "7" and "devSetting" not in form


def test_signed():
    headers = send(WriteVariant(sign=True))[3]
    assert headers["sign"] and headers["requestapp"] == "r"


def test_app_headers_use_real_dev_type():
    headers = send(WriteVariant(app_headers=True))[3]
    assert headers["devtype"] == "11" and headers["minversion"] == ""


def test_api_base_override_only_for_the_write():
    import httpx
    api = FakeAPI()
    api.queue("/dev/getdevModeSettingList", (200, {"code": 200, "data": record()}))
    c = make_client(api)
    hosts = []

    def handler(request):
        hosts.append((request.url.scheme, request.url.host, request.url.path))
        return api.handler(request)

    c._client = httpx.Client(transport=httpx.MockTransport(handler))
    write_port_control(c, "1", 1, {"mode": "manual", "state": True, "speed": 7},
                       variant=WriteVariant(api_base="http://alt.example/api/"))
    write_host = [h for h in hosts if h[2].endswith("/dev/addDevMode")][0]
    read_host = [h for h in hosts if h[2].endswith("/getdevModeSettingList")][0]
    assert write_host == ("http", "alt.example", "/api/dev/addDevMode")
    assert read_host[:2] == ("https", "www.acinfinityserver.com")


def test_knobs_listing():
    assert WriteVariant(name="x", only_update_speed=1, clamp_f=True).knobs() == {"only_update_speed": 1, "clamp_f": True}
