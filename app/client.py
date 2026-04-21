"""AC Infinity cloud API client."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "http://www.acinfinityserver.com/api"
LOGIN_ENDPOINT = f"{API_BASE}/user/appUserLogin"
DEVICES_ENDPOINT = f"{API_BASE}/user/devInfoListAll"
DEV_MODE_SETTING_ENDPOINT = f"{API_BASE}/dev/getdevModeSettingList"
DEV_SETTING_ENDPOINT = f"{API_BASE}/dev/getDevSetting"
HISTORY_ENDPOINT = f"{API_BASE}/log/dataPage"


def _normalize_password(secret: str) -> str:
    """Strip BOM / smart quotes — common copy-paste issues from password managers."""
    s = secret.strip().lstrip("\ufeff")
    return (
        s.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )


def _login_fcm_token() -> str:
    """Match ``LoginModel``: ``Android_`` + stored FCM suffix (empty suffix → ``Android_``)."""
    raw = (os.environ.get("ACINFINITY_FCM_TOKEN") or "").strip()
    if raw:
        return raw if raw.startswith("Android_") else f"Android_{raw}"
    return "Android_"


def _login_attempt_variants(email: str, password: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(em: str, pw: str) -> None:
        key = (em, pw)
        if key not in seen:
            seen.add(key)
            rows.append(key)

    add(email.strip(), password.strip())
    el = email.strip().lower()
    if el != email.strip():
        add(el, password.strip())
    pn = _normalize_password(password)
    if pn != password.strip():
        add(email.strip(), pn)
        if el != email.strip():
            add(el, pn)
    return rows


def _retryable_login_json(body: dict[str, Any]) -> bool:
    msg = str(body.get("msg") or "").lower()
    return "password" in msg or "incorrect" in msg or body.get("code") == 500


def _login_transport_preference() -> str:
    """Which login shape to try first: ``form`` (legacy dashboard) or ``query`` (Android Retrofit).

    Set ``ACINFINITY_LOGIN_TRANSPORT=query`` to prefer query+fcm first (e.g. if form stops working).
    Default ``form`` keeps behavior that matched early acdash / community clients.
    """
    v = (os.environ.get("ACINFINITY_LOGIN_TRANSPORT") or "form").strip().lower()
    return "query" if v == "query" else "form"


class ACInfinityClient:
    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self.token: str | None = None
        self.last_auth_error: str | None = None
        self._client = httpx.Client(timeout=120.0)
        self._client.headers["Content-Type"] = "application/x-www-form-urlencoded"

    def close(self) -> None:
        self._client.close()

    def _login_post(self, em: str, pw: str, *, use_query: bool) -> dict[str, Any] | None:
        """POST ``user/appUserLogin``: form body (legacy) or query string + fcm (Android)."""
        try:
            if use_query:
                response = self._client.post(
                    LOGIN_ENDPOINT,
                    params={
                        "appEmail": em,
                        "appPasswordl": pw,
                        "fcmToken": _login_fcm_token(),
                    },
                )
            else:
                response = self._client.post(
                    LOGIN_ENDPOINT,
                    data={
                        "appEmail": em,
                        "appPasswordl": pw,
                    },
                )
            response.raise_for_status()
            out = response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("Login POST (%s) failed: %s", "query" if use_query else "form", e)
            return None

        return out if isinstance(out, dict) else None

    def authenticate(self) -> bool:
        """Try form-body login first (original acdash), then Android-style query + ``fcmToken``.

        Cloud behavior has varied: many setups still accept form-only POST; Retrofit uses query params.
        Default order is form → query; set ``ACINFINITY_LOGIN_TRANSPORT=query`` to reverse.
        """
        self.last_auth_error = None
        prefer_query = _login_transport_preference() == "query"
        transport_order: tuple[bool, ...] = (True, False) if prefer_query else (False, True)
        attempts = _login_attempt_variants(self.email, self.password)
        success: dict[str, Any] | None = None

        for i, (em, pw) in enumerate(attempts):
            last_fail_body: dict[str, Any] | None = None
            for use_query in transport_order:
                body = self._login_post(em, pw, use_query=use_query)
                if body is None:
                    continue
                last_fail_body = body
                if body.get("code") == 200:
                    success = body
                    break
                self.last_auth_error = str(body.get("msg") or "Unknown error")
            if success is not None:
                if i > 0:
                    logger.info("Login succeeded after credential variant retry (%d)", i)
                break
            if last_fail_body is None and not self.last_auth_error:
                self.last_auth_error = "Request failed (network or invalid JSON from AC Infinity)"
            if i + 1 < len(attempts) and last_fail_body is not None and _retryable_login_json(last_fail_body):
                continue
            logger.error("Authentication failed: %s", self.last_auth_error)
            return False

        if success is None:
            if not self.last_auth_error:
                self.last_auth_error = "No response from AC Infinity (check network)"
            return False

        self.token = success.get("data", {}).get("appId")
        if not self.token:
            logger.error("No appId in authentication response")
            self.last_auth_error = "No session token (appId) in response"
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

    def history_data_page(
        self,
        dev_id: str,
        time_end: int,
        time_start: int,
        *,
        page_size: int = 1000,
        order_direction: int = 1,
        _retry: bool = True,
    ) -> dict[str, Any] | None:
        """POST log/dataPage — returns ``data`` dict (rows, total, validFrom) or None on failure.

        Retrofit ``LogApi`` uses **@Query** on this POST (no form body). Wide time windows sometimes
        return empty rows if parameters are only in the body; we send **query params** like the app.
        """
        if not self.token and not self.authenticate():
            return None
        params = {
            "appId": self.token,
            "devId": dev_id,
            "time": time_end,
            "endTime": time_start,
            "pageSize": page_size,
            "orderDirection": order_direction,
        }
        headers = {"token": self.token}
        max_attempts = 8
        tried_body_fallback = False
        parsed: dict[str, Any] | None = None

        for attempt in range(max_attempts):
            try:
                response = self._client.post(
                    HISTORY_ENDPOINT,
                    params=params,
                    headers=headers,
                    timeout=120.0,
                )
            except httpx.HTTPError as e:
                logger.error("POST %s failed: %s", HISTORY_ENDPOINT, e)
                return None

            if response.status_code == 401:
                logger.warning("Token expired, re-authenticating")
                self.token = None
                if _retry and self.authenticate():
                    return self.history_data_page(
                        dev_id,
                        time_end,
                        time_start,
                        page_size=page_size,
                        order_direction=order_direction,
                        _retry=False,
                    )
                return None

            try:
                response.raise_for_status()
                parsed = response.json()
            except (httpx.HTTPError, ValueError) as e:
                logger.error("Request to %s: bad response: %s", HISTORY_ENDPOINT, e)
                return None

            if not isinstance(parsed, dict):
                return None
            if parsed.get("code") == 200:
                data = parsed.get("data")
                return data if isinstance(data, dict) else None

            msg = str(parsed.get("msg") or "")
            rate_limited = parsed.get("code") == 500 and "rate" in msg.lower()
            if rate_limited and attempt + 1 < max_attempts:
                time.sleep(min(90.0, 4.0 * (2**attempt)))
                continue
            if rate_limited and not tried_body_fallback:
                tried_body_fallback = True
                try:
                    r2 = self._client.post(
                        HISTORY_ENDPOINT,
                        data=params,
                        headers=headers,
                        timeout=120.0,
                    )
                    r2.raise_for_status()
                    p2 = r2.json()
                except (httpx.HTTPError, ValueError) as e:
                    logger.warning("history dataPage form-body fallback failed: %s", e)
                else:
                    if isinstance(p2, dict) and p2.get("code") == 200:
                        data = p2.get("data")
                        return data if isinstance(data, dict) else None

            logger.warning(
                "history dataPage failed code=%s msg=%s",
                parsed.get("code"),
                parsed.get("msg"),
            )
            return None

        return None
