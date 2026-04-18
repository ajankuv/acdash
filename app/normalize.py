"""Map AC Infinity API payloads to dashboard-friendly structures.

VPD / sensor semantics cross-checked with:
- https://github.com/LukeEvansTech/acinfinity-exporter — controller VPD from ``deviceInfo["vpd"]`` (÷100 for kPa),
  legacy 1-based ``sensorType`` + ``sensorPrecis`` scaling.
- homeassistant-acinfinity — ``vpdnums`` on the controller object, 0-based sensor types, ``sensorPrecision``.
"""

from __future__ import annotations

import math
from typing import Any

# Sensor type IDs (same semantics as acinfinity-exporter)
SENSOR_TYPE_PROBE_TEMP_F = 1
SENSOR_TYPE_PROBE_TEMP_C = 2
SENSOR_TYPE_PROBE_HUMIDITY = 3
SENSOR_TYPE_PROBE_VPD = 4
SENSOR_TYPE_CTRL_TEMP_F = 5
SENSOR_TYPE_CTRL_TEMP_C = 6
SENSOR_TYPE_CTRL_HUMIDITY = 7
SENSOR_TYPE_CTRL_VPD = 8
SENSOR_TYPE_CO2 = 9
SENSOR_TYPE_LIGHT = 10
SENSOR_TYPE_SOIL = 12

SENSOR_TYPE_NAMES: dict[int, str] = {
    SENSOR_TYPE_PROBE_TEMP_F: "probe_temp",
    SENSOR_TYPE_PROBE_TEMP_C: "probe_temp",
    SENSOR_TYPE_PROBE_HUMIDITY: "probe_humidity",
    SENSOR_TYPE_PROBE_VPD: "probe_vpd",
    SENSOR_TYPE_CTRL_TEMP_F: "ctrl_temp",
    SENSOR_TYPE_CTRL_TEMP_C: "ctrl_temp",
    SENSOR_TYPE_CTRL_HUMIDITY: "ctrl_humidity",
    SENSOR_TYPE_CTRL_VPD: "ctrl_vpd",
    SENSOR_TYPE_CO2: "co2",
    SENSOR_TYPE_LIGHT: "light",
    SENSOR_TYPE_SOIL: "soil",
}

TEMPERATURE_SENSORS = {
    SENSOR_TYPE_PROBE_TEMP_F,
    SENSOR_TYPE_PROBE_TEMP_C,
    SENSOR_TYPE_CTRL_TEMP_F,
    SENSOR_TYPE_CTRL_TEMP_C,
}
HUMIDITY_SENSORS = {SENSOR_TYPE_PROBE_HUMIDITY, SENSOR_TYPE_CTRL_HUMIDITY}
VPD_SENSORS = {SENSOR_TYPE_PROBE_VPD, SENSOR_TYPE_CTRL_VPD}
FAHRENHEIT_SENSORS = {SENSOR_TYPE_PROBE_TEMP_F, SENSOR_TYPE_CTRL_TEMP_F}

# Home Assistant / newer app payloads: sensorType is 0-based (see homeassistant-acinfinity const.py)
HA_SENSOR_NAMES: dict[int, str] = {
    0: "probe_temp",
    1: "probe_temp",
    2: "probe_humidity",
    3: "probe_vpd",
    4: "ctrl_temp",
    5: "ctrl_temp",
    6: "ctrl_humidity",
    7: "ctrl_vpd",
    10: "soil",
    11: "co2",
    12: "light",
}
HA_TEMP_TYPES = {0, 1, 4, 5}
HA_TEMP_F_TYPES = {0, 4}
HA_HUM_TYPES = {2, 6}
HA_VPD_TYPES = {3, 7}
HA_SOIL_TYPE = 10
HA_CO2_TYPE = 11
HA_LIGHT_TYPE = 12


def fahrenheit_to_celsius(f: float) -> float:
    return (f - 32) * 5 / 9


def scale_value(value: int | float, precision: int) -> float:
    if precision <= 0:
        return float(value)
    return float(value) / (10**precision)


def _finite_optional(x: float | None) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _sensor_raw_to_value(
    sensor: dict[str, Any], sdata: Any, *, sensor_type: int, ha_scheme: bool
) -> float:
    """UIS uses `sensorPrecis` + exporter-style decimals; HA uses `sensorPrecision` with a different rule."""
    # `sensorPrecis: 0` is common; it means "no extra decimal scaling", not "multiply by 10^0 only".
    # Fall through so VPD types still use hundredths-of-kPa like `vpdnums`.
    precis_val = sensor.get("sensorPrecis")
    if precis_val is not None:
        prec = int(precis_val or 0)
        if prec > 0:
            return scale_value(sdata, prec)
    if sensor.get("sensorPrecision") is not None:
        prec = int(sensor.get("sensorPrecision") or 1)
        data = float(sdata)
        return data / (10 ** (prec - 1)) if prec > 1 else data
    # VPD is stored like controller `vpdnums`: hundredths of kPa when precision keys are absent.
    vpd_types = HA_VPD_TYPES if ha_scheme else VPD_SENSORS
    if sensor_type in vpd_types:
        return float(sdata) / 100.0
    return scale_value(sdata, 0)


def normalize_devices(raw_devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    controllers: list[dict[str, Any]] = []
    for device in raw_devices:
        cid = str(device.get("devId", ""))
        if not cid:
            continue
        name = device.get("devName", "Unknown")
        info = device.get("deviceInfo") or {}
        ports = [_normalize_port(p) for p in (info.get("ports") or [])]
        sensors_list = _normalize_sensors(info.get("sensors") or [], cid)
        temp_c = _scaled_optional(_pick_field(device, info, "temperature"), div=100.0)
        humidity_pct = _scaled_optional(_pick_field(device, info, "humidity"), div=100.0)
        vpd_kpa, vpd_is_estimate = _resolve_vpd(
            device, info, sensors_list, temp_c, humidity_pct
        )
        temp_c = _finite_optional(temp_c)
        humidity_pct = _finite_optional(humidity_pct)
        vpd_kpa = _finite_optional(vpd_kpa)
        controllers.append(
            {
                "id": cid,
                "name": name,
                "firmware": device.get("firmwareVersion") or "—",
                "hardware": device.get("hardwareVersion") or "—",
                "wifi": device.get("wifiName") or "—",
                "temp_c": temp_c,
                "humidity_pct": humidity_pct,
                "vpd_kpa": vpd_kpa,
                "vpd_is_estimate": vpd_is_estimate,
                "temp_trend": info.get("tTrend"),
                "humidity_trend": info.get("hTrend"),
                "mode": info.get("curMode"),
                "ports": ports,
                "sensors": sensors_list,
            }
        )
    return controllers


def _scaled_optional(raw: Any, *, div: float) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw) / div
    except (TypeError, ValueError):
        return None


def _pick_field(device: dict[str, Any], info: dict[str, Any], key: str) -> Any:
    """Read from device root and deviceInfo. Prefer a non-null value so root `null` does not hide info."""
    dev_has = key in device
    inf_has = key in info
    dev = device[key] if dev_has else None
    inf = info[key] if inf_has else None
    if dev is not None:
        return dev
    if inf is not None:
        return inf
    if dev_has:
        return dev
    if inf_has:
        return inf
    return None


def _is_ha_sensor_scheme(sensor: dict[str, Any]) -> bool:
    """0-based sensorType (homeassistant-acinfinity) unless legacy `sensorPrecis` has a value.

    Cloud payloads often omit both precision keys; requiring `sensorPrecision` mis-maps type 3 as legacy humidity.
    Explicit JSON null for `sensorPrecis` still counts as modern.
    """
    if "sensorPrecis" in sensor and sensor["sensorPrecis"] is None:
        return True
    if sensor.get("sensorPrecis") is not None:
        return False
    if "sensorPrecision" in sensor:
        return True
    return "sensorPrecis" not in sensor


def _estimated_vpd_kpa(temp_c: float | None, rh_pct: float | None) -> float | None:
    """Air VPD (kPa) from T/RH when the cloud payload has no VPD field — horticulture approximation."""
    if temp_c is None or rh_pct is None:
        return None
    t = float(temp_c)
    h = float(rh_pct)
    if h < 0 or h > 100:
        return None
    # If root temperature was mis-read as °C but is actually °F (e.g. 72), convert for the estimate.
    if 45 < t <= 130:
        t = fahrenheit_to_celsius(t)
    if t < -20 or t > 60:
        return None
    svp = 0.6108 * math.exp((17.27 * t) / (t + 237.3))
    vpd = svp * (1.0 - h / 100.0)
    if vpd < 0 or vpd > 10:
        return None
    return round(vpd, 2)


def _resolve_vpd(
    device: dict[str, Any],
    info: dict[str, Any],
    sensors_list: list[dict[str, Any]],
    temp_c: float | None,
    humidity_pct: float | None,
) -> tuple[float | None, bool]:
    api = _controller_vpd_kpa(device, info, sensors_list)
    if api is not None:
        return api, False
    est = _estimated_vpd_kpa(temp_c, humidity_pct)
    if est is not None:
        return est, True
    return None, False


def _vpd_from_raw_sensors(sensors: list[dict[str, Any]]) -> float | None:
    """Read VPD from the raw `sensors` array using known type IDs.

    HA integration uses 0-based types 3 / 7; legacy exporter uses 4 / 8. We try both so
    `_is_ha_sensor_scheme` mistakes (e.g. `sensorPrecis: 0` forcing legacy) do not hide VPD.
    """
    for sensor in sensors:
        st_raw = sensor.get("sensorType")
        sdata = sensor.get("sensorData")
        if st_raw is None or sdata is None:
            continue
        try:
            st = int(st_raw)
        except (TypeError, ValueError):
            continue
        for ha_scheme, types in (
            (True, HA_VPD_TYPES),
            (False, VPD_SENSORS),
        ):
            if st not in types:
                continue
            try:
                v = float(_sensor_raw_to_value(sensor, sdata, sensor_type=st, ha_scheme=ha_scheme))
            except (TypeError, ValueError):
                continue
            if 0 < v <= 10:
                return round(v, 2)
    return None


def _controller_vpd_kpa(
    device: dict[str, Any],
    info: dict[str, Any],
    sensors_list: list[dict[str, Any]],
) -> float | None:
    """Controller VPD in kPa: integer hundredths from the cloud (÷100).

    Exporter (LukeEvansTech/acinfinity-exporter) uses ``deviceInfo["vpd"]``; HA integration uses ``vpdnums``.
    Prefer ``vpd`` first when both exist so we match exporter-style payloads.
    """
    for key in (
        "vpd",
        "vpdnums",
        "vpdNums",
        "vpdNum",
        "vpdKpa",
        "vpdValue",
        "targetVpd",
    ):
        raw = _pick_field(device, info, key)
        if raw is None:
            continue
        v = _scaled_optional(raw, div=100.0)
        # UIS often sends 0 when VPD is unavailable — prefer sensor / T–RH estimate.
        if v is not None and v > 0:
            return v
        try:
            f = float(raw)
        except (TypeError, ValueError):
            continue
        if 0 < f < 50:
            return f

    for s in sensors_list:
        if s.get("type") in ("probe_vpd", "ctrl_vpd") and s.get("value") is not None:
            vf = float(s["value"])
            if vf > 0:
                return vf

    raw_list = info.get("sensors") or []
    if isinstance(raw_list, list):
        from_raw = _vpd_from_raw_sensors(raw_list)
        if from_raw is not None:
            return from_raw
    return None


def _normalize_port(port_data: dict[str, Any]) -> dict[str, Any]:
    port_num = str(port_data.get("port", ""))
    resistance = port_data.get("portResistance")
    connected = None
    if resistance is not None:
        connected = resistance != 65535
    # UIS list payload uses loadState (and loadType); some paths may still expose state.
    load_state = port_data.get("loadState")
    if load_state is None:
        load_state = port_data.get("state")
    return {
        "port": port_num,
        "name": port_data.get("portName", "Unknown"),
        "speed": port_data.get("speak"),
        "online": port_data.get("online"),
        "state": load_state,
        "mode": port_data.get("curMode"),
        "connected": connected,
        "overcurrent": port_data.get("overcurrentStatus"),
        "abnormal": port_data.get("abnormalState"),
        "load_type": port_data.get("loadType"),
        "device_type": port_data.get("deviceType"),
    }


def _normalize_sensors(sensors: list[dict[str, Any]], controller_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sensor in sensors:
        stype = sensor.get("sensorType")
        sdata = sensor.get("sensorData")
        if stype is None or sdata is None:
            continue
        stype = int(stype)
        unit = int(sensor.get("sensorUnit", 1))
        ha = _is_ha_sensor_scheme(sensor)
        type_name = (
            HA_SENSOR_NAMES.get(stype, f"type_{stype}")
            if ha
            else SENSOR_TYPE_NAMES.get(stype, f"type_{stype}")
        )
        value = _sensor_raw_to_value(sensor, sdata, sensor_type=stype, ha_scheme=ha)
        port = str(sensor.get("accessPort", sensor.get("sensorPort", sensor.get("port", "0"))))

        label = type_name.replace("_", " ").title()
        display_value: float | None = value
        suffix = ""

        if ha:
            if stype in HA_TEMP_TYPES:
                if stype in HA_TEMP_F_TYPES or unit == 0:
                    display_value = fahrenheit_to_celsius(value)
                suffix = "°C"
            elif stype in HA_HUM_TYPES:
                suffix = "%"
            elif stype in HA_VPD_TYPES:
                suffix = " kPa"
            elif stype == HA_CO2_TYPE:
                suffix = " ppm"
            elif stype == HA_LIGHT_TYPE:
                suffix = "%"
            elif stype == HA_SOIL_TYPE:
                suffix = "%"
        else:
            if stype in TEMPERATURE_SENSORS:
                if stype in FAHRENHEIT_SENSORS or unit == 0:
                    display_value = fahrenheit_to_celsius(value)
                suffix = "°C"
            elif stype in HUMIDITY_SENSORS:
                suffix = "%"
            elif stype in VPD_SENSORS:
                suffix = " kPa"
            elif stype == SENSOR_TYPE_CO2:
                suffix = " ppm"
            elif stype == SENSOR_TYPE_LIGHT:
                suffix = "%"
            elif stype == SENSOR_TYPE_SOIL:
                suffix = "%"

        is_temp = (ha and stype in HA_TEMP_TYPES) or (not ha and stype in TEMPERATURE_SENSORS)

        out.append(
            {
                "controller_id": controller_id,
                "port": port,
                "type": type_name,
                "label": label,
                "value": display_value,
                "suffix": suffix,
                "is_temp": is_temp,
            }
        )
    return out
