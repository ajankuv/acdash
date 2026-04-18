"""AC Infinity cloud API client."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "http://www.acinfinityserver.com/api"
LOGIN_ENDPOINT = f"{API_BASE}/user/appUserLogin"
DEVICES_ENDPOINT = f"{API_BASE}/user/devInfoListAll"
DEV_MODE_SETTING_ENDPOINT = f"{API_BASE}/dev/getdevModeSettingList"
DEV_SETTING_ENDPOINT = f"{API_BASE}/dev/getDevSetting"


class ACInfinityClient:
    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self.token: str | None = None
        self._client = httpx.Client(timeout=30.0)
        self._client.headers["Content-Type"] = "application/x-www-form-urlencoded"

    def close(self) -> None:
        self._client.close()

    def authenticate(self) -> bool:
        try:
            response = self._client.post(
                LOGIN_ENDPOINT,
                data={
                    "appEmail": self.email,
                    "appPasswordl": self.password,
                },
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.error("Authentication request failed: %s", e)
            return False

        if data.get("code") != 200:
            logger.error("Authentication failed: %s", data.get("msg", "Unknown error"))
            return False

        self.token = data.get("data", {}).get("appId")
        if not self.token:
            logger.error("No appId in authentication response")
            return False

        logger.info("Authenticated with AC Infinity API")
        return True

    def _post_with_token(self, url: str, form: dict[str, Any], *, _retry: bool = True) -> dict[str, Any] | None:
        if not self.token and not self.authenticate():
            return None

        try:
            response = self._client.post(
                url,
                data=form,
                headers={"token": self.token},
            )
        except httpx.HTTPError as e:
            logger.error("POST %s failed: %s", url, e)
            return None

        if response.status_code == 401:
            logger.warning("Token expired, re-authenticating")
            self.token = None
            if _retry and self.authenticate():
                return self._post_with_token(url, form, _retry=False)
            return None

        try:
            response.raise_for_status()
            parsed: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.error("Request to %s: bad response: %s", url, e)
            return None

        return parsed

    def get_dev_info_list_all_full(self) -> dict[str, Any] | None:
        """Full JSON body from devInfoListAll: ``{code, msg, data}``."""
        if not self.token and not self.authenticate():
            return None
        return self._post_with_token(DEVICES_ENDPOINT, {"userId": self.token})

    def get_devices(self) -> list[dict[str, Any]]:
        data = self.get_dev_info_list_all_full()
        if not data:
            return []

        if data.get("code") != 200:
            logger.error("Failed to get devices: %s", data.get("msg", "Unknown error"))
            return []

        devices = data.get("data", [])
        logger.debug("Retrieved %d devices", len(devices))
        return devices if isinstance(devices, list) else []

    def get_dev_mode_setting_list(self, dev_id: str | int, port: int) -> dict[str, Any] | None:
        """Full JSON body from getdevModeSettingList."""
        return self._post_with_token(
            DEV_MODE_SETTING_ENDPOINT,
            {"devId": str(dev_id), "port": port},
        )

    def get_dev_setting(self, dev_id: str | int, port: int) -> dict[str, Any] | None:
        """Full JSON body from getDevSetting (port 0 = controller-level per AC Infinity apps)."""
        return self._post_with_token(
            DEV_SETTING_ENDPOINT,
            {"devId": str(dev_id), "port": port},
        )
