"""Shared test doubles: a scripted AC Infinity API behind httpx.MockTransport."""
from __future__ import annotations

import threading
import urllib.parse

import httpx

from app.client import ACInfinityClient


class FakeAPI:
    """Minimal scripted AC Infinity API behind httpx.MockTransport."""

    def __init__(self) -> None:
        self.logins = 0
        self.calls: list[tuple[str, dict, dict, dict]] = []  # (path, query, form, headers)
        self.password = "right"
        self.scripts: dict[str, list[tuple[int, dict]]] = {}  # path suffix → queued responses
        self.token_seq = 0
        self.expired_tokens: set[str] = set()
        self.lock = threading.Lock()

    def queue(self, path: str, *responses: tuple[int, dict]) -> None:
        self.scripts.setdefault(path, []).extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = dict(request.url.params)
        form = dict(urllib.parse.parse_qsl(request.content.decode())) if request.content else {}
        with self.lock:
            self.calls.append((path, query, form, dict(request.headers)))
            if path.endswith("/user/appUserLogin"):
                self.logins += 1
                fields = {**query, **form}
                if fields.get("appPasswordl") != self.password:
                    return httpx.Response(200, json={"code": 10001, "msg": "Incorrect Password"})
                self.token_seq += 1
                return httpx.Response(200, json={"code": 200, "data": {
                    "appId": f"tok-{self.token_seq}", "secretId": "s", "requestApp": "r"}})
            if request.headers.get("token") in self.expired_tokens:
                return httpx.Response(200, json={"code": 10003, "msg": "Login Expired"})
            for suffix, queue in self.scripts.items():
                if path.endswith(suffix) and queue:
                    status, body = queue.pop(0)
                    return httpx.Response(status, json=body)
            return httpx.Response(200, json={"code": 200, "msg": "success", "data": []})

    def count(self, suffix: str) -> int:
        return sum(1 for c in self.calls if c[0].endswith(suffix))


def make_client(api: "FakeAPI", password: str = "right", email: str = "user@example.com") -> ACInfinityClient:
    c = ACInfinityClient(email, password)
    c._client.close()
    c._client = httpx.Client(transport=httpx.MockTransport(api.handler))
    c.slept = []  # type: ignore[attr-defined]
    c._sleep = lambda s: c.slept.append(s)  # type: ignore[attr-defined]
    return c
