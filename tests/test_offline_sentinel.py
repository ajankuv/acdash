"""-32768 is AC Infinity's 'no reading' value for offline controllers."""
from app.history import history_row_to_point
from app.normalize import normalize_devices


def _device(temp, hum, vpd=0, sensors=None):
    return {"devId": "1", "devName": "T", "deviceInfo": {
        "temperature": temp, "humidity": hum, "vpdnums": vpd, "ports": [], "sensors": sensors or []}}


def test_offline_controller_has_no_readings():
    (c,) = normalize_devices([_device(-32768, -32768, -32768)])
    assert c["temp_c"] is None
    assert c["humidity_pct"] is None
    assert c["vpd_kpa"] is None


def test_online_controller_unchanged():
    (c,) = normalize_devices([_device(2450, 5520, 138)])
    assert c["temp_c"] == 24.5
    assert c["humidity_pct"] == 55.2
    assert c["vpd_kpa"] == 1.38


def test_offline_sensor_skipped():
    sensors = [{"sensorType": 0, "sensorData": -32768, "sensorUnit": 1, "accessPort": 1},
               {"sensorType": 2, "sensorData": 5500, "sensorUnit": 1, "sensorPrecision": 3, "accessPort": 1}]
    (c,) = normalize_devices([_device(2450, 5520, 138, sensors)])
    assert all(s["value"] is None or s["value"] > -1000 for s in c["sensors"])
    assert len(c["sensors"]) == 1


def test_history_row_sentinel_is_missing():
    p = history_row_to_point({"createTime": 1_700_000_000, "temperature": -32768,
                              "humidity": 5500, "vpdNums": -32768})
    assert p["temp_c"] is None and p["vpd_kpa"] is None and p["rh"] == 55.0
