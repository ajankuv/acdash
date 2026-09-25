"""AC Infinity cloud API client."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "http://www.acinfinityserver.com/api"
# Override for CI (points the container at a fake AC Infinity server). Unset → real cloud.
API_BASE = (os.environ.get("ACINFINITY_API_BASE") or "").strip().rstrip("/") or DEFAULT_API_BASE
LOGIN_ENDPOINT = f"{API_BASE}/user/appUserLogin"
DEVICES_ENDPOINT = f"{API_BASE}/user/devInfoListAll"
DEV_MODE_SETTING_ENDPOINT = f"{API_BASE}/dev/getdevModeSettingList"
DEV_SETTING_ENDPOINT = f"{API_BASE}/dev/getDevSetting"
HISTORY_ENDPOINT = f"{API_BASE}/log/dataPage"
EVENT_LOG_ENDPOINT = f"{API_BASE}/log/logdataByAll"
ADD_DEV_MODE_ENDPOINT = f"{API_BASE}/dev/addDevMode"
AUTOMATIONS_ENDPOINT = f"{API_BASE}/version=2.0/dev/getGroups"
AUTOMATION_TOGGLE_ENDPOINT = f"{API_BASE}/version=2.0/dev/updateGroupsIsOn"
AUTOMATION_DELETE_ENDPOINT = f"{API_BASE}/version=2.0/dev/delByid"
AUTOMATION_CREATE_ENDPOINT = f"{API_BASE}/version=2.0/dev/addGroups"


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
    # The AC Infinity apps silently truncate passwords to 25 characters, so an account
    # created in the app with a longer password only accepts the first 25. Tried last,
    # so logins that work today keep the exact same attempt order.
    for em, pw in list(rows):
        if len(pw) > MAX_PASSWORD_LEN:
            add(em, pw[:MAX_PASSWORD_LEN])
    return rows


MAX_PASSWORD_LEN = 25
LOGIN_COOLDOWN_SECS = 300.0
RATE_LIMIT_BACKOFF_SECS = 10.0
LOG_ENDPOINT_SPACING_SECS = 2.5
AI_DEV_TYPES = frozenset({20, 21, 22})
SESSION_EXPIRED_CODES = frozenset({10003})
RATE_LIMIT_CODE = 999998


def _classify(status_code: int, body: Any) -> str:
    """Classify an API response: ``ok`` | ``expired`` | ``rate_limited`` | ``error``.

    AC Infinity signals an expired session several ways: HTTP 401, ``code 10003`` inside an
    HTTP 200, or ``403`` (HTTP or body code) with a "login expired" / "login again" message.
    """
    code = body.get("code") if isinstance(body, dict) else None
    msg = str(body.get("msg") or "").lower() if isinstance(body, dict) else ""
    login_msg = "login expired" in msg or "login again" in msg
    if status_code == 401 or code in SESSION_EXPIRED_CODES:
        return "expired"
    if (status_code == 403 or code == 403) and login_msg:
        return "expired"
    if code == RATE_LIMIT_CODE:
        return "rate_limited"
    if status_code == 200 and code == 200:
        return "ok"
    return "error"


def _is_credential_refusal(body: dict[str, Any] | None) -> bool:
    """True when login was refused for bad credentials (not a network/server problem)."""
    if not isinstance(body, dict):
        return False
    msg = str(body.get("msg") or "").lower()
    return body.get("code") == 10001 or "password" in msg or "incorrect" in msg


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
        self.last_request_error: str | None = None
        # Returned by login; used only for optional request signing (see control write path).
        self.secret_id: str | None = None
        self.request_app: str | None = None
        self._client = httpx.Client(timeout=120.0)
        self._client.headers["Content-Type"] = "application/x-www-form-urlencoded"
        # One login at a time per client, so parallel requests after an expiry don't stampede.
        self._auth_lock = threading.RLock()
        self._login_blocked_until = 0.0
        self._log_lock = threading.Lock()
        self._last_log_call = 0.0
        self._dev_types: dict[str, int] = {}
        # Injectable for tests.
        self._sleep = time.sleep
        self._monotonic = time.monotonic

    def close(self) -> None:
        self._client.close()

    def _v2_headers(self, dev_id: str | int | None = None) -> dict[str, str]:
        """Extra headers required for version=2.0/dev/* endpoints.

        AI+ controllers (devType 20/21/22) need ``minversion: 3.5`` exactly and their real
        devType; everything else keeps the headers acdash has always sent.
        """
        dev_type = self._dev_types.get(str(dev_id)) if dev_id is not None else None
        if dev_type in AI_DEV_TYPES:
            return {"token": self.token or "", "devType": str(dev_type), "minversion": "3.5"}
        return {
            "token": self.token or "",
            "devType": "11",
            "minversion": "0.0.0",
        }

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

        After a credential refusal, further logins are skipped for ``LOGIN_COOLDOWN_SECS`` (the
        refusal message stays in ``last_auth_error``) so a wrong password isn't hammered every refresh.
        """
        with self._auth_lock:
            if self._monotonic() < self._login_blocked_until:
                logger.debug("Login skipped (cool-down after refusal): %s", self.last_auth_error)
                return False
            ok, refused = self._authenticate_once()
            if not ok and refused:
                self._login_blocked_until = self._monotonic() + LOGIN_COOLDOWN_SECS
            return ok

    def _authenticate_once(self) -> tuple[bool, bool]:
        """One full login pass. Returns ``(ok, refused_for_credentials)``."""
        self.last_auth_error = None
        prefer_query = _login_transport_preference() == "query"
        transport_order: tuple[bool, ...] = (True, False) if prefer_query else (False, True)
        attempts = _login_attempt_variants(self.email, self.password)
        success: dict[str, Any] | None = None
        last_fail_body: dict[str, Any] | None = None

        for i, (em, pw) in enumerate(attempts):
            last_fail_body = None
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
            return False, _is_credential_refusal(last_fail_body)

        if success is None:
            if not self.last_auth_error:
                self.last_auth_error = "No response from AC Infinity (check network)"
            return False, _is_credential_refusal(last_fail_body)

        data = success.get("data") or {}
        self.token = data.get("appId")
        if not self.token:
            logger.error("No appId in authentication response")
            self.last_auth_error = "No session token (appId) in response"
            return False, False
        self.secret_id = data.get("secretId")
        self.request_app = data.get("requestApp")

        logger.info("Authenticated with AC Infinity API")
        return True, False

    def _ensure_token(self) -> bool:
        if self.token:
            return True
        with self._auth_lock:
            return bool(self.token) or self.authenticate()

    def _renew_session(self, stale_token: str | None) -> bool:
        """Log in again after an expiry. If another thread already renewed, reuse its token."""
        with self._auth_lock:
            if self.token and self.token != stale_token:
                return True
            self.token = None
            logger.warning("AC Infinity session expired, re-authenticating")
            return self.authenticate()

    def _space_log_calls(self) -> None:
        """Keep log/* endpoint calls ≥ LOG_ENDPOINT_SPACING_SECS apart (they rate-limit hard)."""
        with self._log_lock:
            wait = self._last_log_call + LOG_ENDPOINT_SPACING_SECS - self._monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last_log_call = self._monotonic()

    def _request(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        header_fn: Any = None,
        retry_on_expired: bool = True,
        retry_on_rate_limit: bool | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        """POST with the session token; handles expiry and ``999998`` rate limiting.

        - Expired session → log in again; retry once when ``retry_on_expired`` (reads). Writes
          pass ``False``: the session is renewed but the write is NOT resent, and the expired
          body is returned so the caller can ask the user to retry.
        - ``999998`` → wait ``RATE_LIMIT_BACKOFF_SECS`` and retry once.
        Returns the parsed JSON body, or None on network/HTTP/JSON failure.
        """
        if retry_on_rate_limit is None:
            retry_on_rate_limit = retry_on_expired  # writes opt out of both automatic resends
        if not self._ensure_token():
            return None
        expired_retried = False
        rate_retried = not retry_on_rate_limit
        while True:
            token = self.token
            hdrs = dict(header_fn() if header_fn else {"token": token or ""})
            if headers:
                hdrs.update(headers)
            # Some endpoints repeat the token as userId/appId — keep them in sync after a renewal.
            body_data = _with_token(data, token)
            query = _with_token(params, token)
            try:
                kwargs: dict[str, Any] = {"headers": hdrs}
                if body_data is not None:
                    kwargs["data"] = body_data
                if query is not None:
                    kwargs["params"] = query
                if timeout is not None:
                    kwargs["timeout"] = timeout
                response = self._client.post(url, **kwargs)
            except httpx.HTTPError as e:
                logger.error("POST %s failed: %s", url, e)
                return None
            except RuntimeError as e:  # client closed underneath us (credentials just changed)
                logger.warning("POST %s aborted: %s", url, e)
                return None

            try:
                parsed: Any = response.json()
            except ValueError:
                parsed = None
            kind = _classify(response.status_code, parsed)

            if kind == "expired":
                # Renew once per call: on the retry's expiry, don't log in again for nothing.
                renewed = False if expired_retried else self._renew_session(token)
                if retry_on_expired and not expired_retried and renewed:
                    expired_retried = True
                    continue
                if isinstance(parsed, dict):
                    return parsed
                return {"code": 10003, "msg": "Login Expired"} if response.status_code == 401 else None

            if kind == "rate_limited" and not rate_retried:
                rate_retried = True
                logger.warning("AC Infinity rate limiting %s; retrying in %.0fs", url, RATE_LIMIT_BACKOFF_SECS)
                self._sleep(RATE_LIMIT_BACKOFF_SECS)
                continue

            if response.status_code >= 400 and not isinstance(parsed, dict):
                logger.error("Request to %s: HTTP %s", url, response.status_code)
                return None
            if not isinstance(parsed, dict):
                logger.error("Request to %s: bad response (not JSON object)", url)
                return None
            return parsed

    def _post_with_token(self, url: str, form: dict[str, Any], *, _retry: bool = True) -> dict[str, Any] | None:
        return self._request(url, data=form, retry_on_expired=_retry)

    def get_dev_info_list_all_full(self) -> dict[str, Any] | None:
        """Full JSON body from devInfoListAll: ``{code, msg, data}``."""
        if not self._ensure_token():
            return None
        return self._request(DEVICES_ENDPOINT, data={"userId": self.token})

    def get_devices(self) -> list[dict[str, Any]]:
        data = self.get_dev_info_list_all_full()
        if not data:
            return []

        if data.get("code") != 200:
            logger.error("Failed to get devices: %s", data.get("msg", "Unknown error"))
            return []

        devices = data.get("data", [])
        if not isinstance(devices, list):
            return []
        for d in devices:
            if isinstance(d, dict) and d.get("devId") is not None:
                try:
                    self._dev_types[str(d["devId"])] = int(d.get("devType"))
                except (TypeError, ValueError):
                    pass
        logger.debug("Retrieved %d devices", len(devices))
        return devices

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

    def set_port_mode(
        self,
        dev_id: str | int,
        port: int,
        payload: dict[str, Any],
        *,
        transport: str = "query",
        sign: bool = False,
        _retry: bool = True,
    ) -> dict[str, Any] | None:
        """POST dev/addDevMode — write port mode settings.

        ``transport="query"`` (default): parameters in the **query string**, like the original
        Retrofit ``@QueryMap`` / dalinicus client. ``transport="form"``: form-urlencoded body,
        like ober37 / keithah / app 2.0.8. The ``payload`` comes from control.build_write_payload.

        ``sign=True`` adds the app 2.0.8 request-signing headers (see ``sign_headers``).
        On an expired session the session is renewed but the write is NOT resent (the
        caller gets the expired body back and asks the user to retry).
        """
        fields = {**payload, "devId": str(dev_id), "externalPort": int(port)}
        extra = {"User-Agent": "okhttp/4.12.0"}

        def headers() -> dict[str, str]:
            h = {"token": self.token or ""}
            if sign:
                h.update(sign_headers(self.token or "", self.secret_id or "", self.request_app or ""))
            return h

        if transport == "form":
            return self._request(
                ADD_DEV_MODE_ENDPOINT, data=fields, headers=extra, header_fn=headers, retry_on_expired=False
            )
        return self._request(
            ADD_DEV_MODE_ENDPOINT, params=fields, headers=extra, header_fn=headers, retry_on_expired=False
        )

    def get_automations_raw(self, dev_id: str | int) -> list[dict[str, Any]]:
        """POST version=2.0/dev/getGroups — raw named automation program list."""
        body = self._request(
            AUTOMATIONS_ENDPOINT,
            data={"devId": str(dev_id), "userId": self.token},
            header_fn=lambda: self._v2_headers(dev_id),
        )
        if body is None:
            logger.error("getGroups failed")
            return []
        if body.get("code") != 200:
            logger.warning("getGroups non-200: %s", body.get("msg"))
            return []
        data = body.get("data") or []
        return data if isinstance(data, list) else []

    def toggle_automation_raw(
        self, dev_id: str | int, adv_id: str | int, *, is_on: bool
    ) -> dict[str, Any] | None:
        """POST version=2.0/dev/updateGroupsIsOn."""
        return self._request(
            AUTOMATION_TOGGLE_ENDPOINT,
            data={
                "devId": str(dev_id),
                "advId": str(adv_id),
                "isflag": 1 if is_on else 0,
                "isDel": 0,
            },
            header_fn=lambda: self._v2_headers(dev_id),
            retry_on_expired=False,
        )

    def delete_automation_raw(self, dev_id: str | int, adv_id: str | int) -> dict[str, Any] | None:
        """POST version=2.0/dev/delByid."""
        return self._request(
            AUTOMATION_DELETE_ENDPOINT,
            data={"devId": str(dev_id), "advId": str(adv_id)},
            header_fn=lambda: self._v2_headers(dev_id),
            retry_on_expired=False,
        )

    def create_automation_raw(
        self, dev_id: str | int, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """POST version=2.0/dev/addGroups."""
        return self._request(
            AUTOMATION_CREATE_ENDPOINT,
            data={**payload, "devId": str(dev_id), "userId": self.token},
            header_fn=lambda: self._v2_headers(dev_id),
            retry_on_expired=False,
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
        Calls are spaced ≥ 2.5 s; ``999998`` backs off once (in ``_request``); the legacy
        ``code 500 + "rate"`` variant keeps its exponential backoff and form-body fallback.
        """
        self.last_request_error = None
        if not self._ensure_token():
            return None
        params = {
            "appId": self.token,
            "devId": dev_id,
            "time": time_end,
            "endTime": time_start,
            "pageSize": page_size,
            "orderDirection": order_direction,
        }
        max_attempts = 8
        tried_body_fallback = False

        for attempt in range(max_attempts):
            self._space_log_calls()
            parsed = self._request(HISTORY_ENDPOINT, params=params, retry_on_expired=_retry, timeout=120.0)
            if parsed is None:
                return None
            if parsed.get("code") == 200:
                data = parsed.get("data")
                return data if isinstance(data, dict) else None

            msg = str(parsed.get("msg") or "")
            rate_limited = parsed.get("code") == 500 and "rate" in msg.lower()
            if rate_limited and attempt + 1 < max_attempts:
                self._sleep(min(90.0, 4.0 * (2**attempt)))
                continue
            if rate_limited and not tried_body_fallback:
                tried_body_fallback = True
                self._space_log_calls()
                p2 = self._request(HISTORY_ENDPOINT, data=params, retry_on_expired=False, timeout=120.0)
                if isinstance(p2, dict) and p2.get("code") == 200:
                    data = p2.get("data")
                    return data if isinstance(data, dict) else None

            if parsed.get("code") == RATE_LIMIT_CODE:
                self.last_request_error = "AC Infinity is rate limiting — try again shortly"
            logger.warning(
                "history dataPage failed code=%s msg=%s",
                parsed.get("code"),
                parsed.get("msg"),
            )
            return None

        return None

    def event_log_page(
        self, dev_id: str, time_newer: int, time_older: int, *, cursor: int | str = 0, page_size: int = 200
    ) -> dict[str, Any] | None:
        """POST log/logdataByAll — the app's event log ("Logs" tab), newest first.

        Query params like the app (misterboe docs/api/history.md): paginate by passing the last
        row's ``id`` as ``id``. Returns ``data`` (``rows``, ``total``) or None. Read-only.
        """
        self.last_request_error = None
        if not self._ensure_token():
            return None
        self._space_log_calls()
        parsed = self._request(
            EVENT_LOG_ENDPOINT,
            params={
                "appId": self.token,
                "devId": dev_id,
                "id": cursor,
                "time": time_newer,
                "endTime": time_older,
                "pageSize": page_size,
                "orderDirection": 1,
            },
        )
        if not parsed or parsed.get("code") != 200:
            if parsed and parsed.get("code") == RATE_LIMIT_CODE:
                self.last_request_error = "AC Infinity is rate limiting — try again shortly"
            return None
        data = parsed.get("data")
        return data if isinstance(data, dict) else None


SIGN_APP_VERSION = "2.0.8"


def _md5(text: str) -> str:
    import hashlib

    return hashlib.md5(text.encode("utf-8")).hexdigest()  # noqa: S324 — protocol requirement


def sign_headers(
    token: str, secret_id: str, request_app: str, *, request_id: str | None = None, version: str = SIGN_APP_VERSION
) -> dict[str, str]:
    """App 2.0.8 request signing (decompiled; HA issue #157, Backroads4Me fork).

    ``sign = md5(md5(token + version) + md5(secretId + requestApp + requestId))`` with
    ``requestId`` = epoch milliseconds. Opt-in only (``ACINFINITY_SIGN_WRITES=1``) — unverified
    against acdash's own controllers.
    """
    rid = request_id or str(int(time.time() * 1000))
    sign = _md5(_md5(token + version) + _md5(secret_id + request_app + rid))
    return {"sign": sign, "requestApp": request_app, "requestId": rid, "version": version}


def _with_token(fields: dict[str, Any] | None, token: str | None) -> dict[str, Any] | None:
    """Copy ``fields`` with ``userId``/``appId`` refreshed to the current token."""
    if fields is None:
        return None
    out = dict(fields)
    for key in ("userId", "appId"):
        if key in out:
            out[key] = token
    return out
