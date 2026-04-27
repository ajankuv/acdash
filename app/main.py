"""FastAPI app: AC Infinity dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Union
from urllib.parse import urljoin, urlunparse

from dotenv import dotenv_values
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import storage
from app.client import ACInfinityClient
from app.collector import COLLECTOR_INTERVAL, collector_loop
from app.debug_bundle import collect_debug_bundle
from app.history import fetch_history_for_chart, thin_points
from app.normalize import normalize_devices

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE_PATH = Path(os.getenv("ENV_FILE_PATH", str(BASE_DIR / ".env")))

env = Environment(
    loader=FileSystemLoader(BASE_DIR / "templates"),
    autoescape=select_autoescape(["html", "xml"]),
)

CACHE_SECONDS = float(os.getenv("CACHE_SECONDS", "45"))

_cache: dict[str, object] = {
    "at": 0.0,
    "controllers": [],
    "error": None,
}

_HISTORY_CACHE_TTL = float(os.getenv("HISTORY_CACHE_SECONDS", "300"))
_history_cache: dict[tuple[str, int], dict[str, Any]] = {}


def _get_history_cache(dev_id: str, hours: float) -> dict[str, Any] | None:
    entry = _history_cache.get((dev_id, round(hours)))
    if entry and (time.monotonic() - entry["at"]) < _HISTORY_CACHE_TTL:
        return entry["data"]
    return None


def _set_history_cache(dev_id: str, hours: float, data: dict[str, Any]) -> None:
    _history_cache[(dev_id, round(hours))] = {"at": time.monotonic(), "data": data}


def _get_controllers_for_collector() -> list[dict]:
    controllers, _, _ = get_cached_controllers()
    return controllers


@asynccontextmanager
async def lifespan(_app: FastAPI):  # noqa: RUF029
    storage.init_db()
    task = asyncio.create_task(collector_loop(_get_controllers_for_collector))
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="AC Dash", version="0.1.0", lifespan=lifespan)


def _client_dashboard_urls(request: Request) -> dict[str, Any]:
    """Build absolute URLs as the browser should call them (avoids broken relative fetch in subpaths)."""
    u = request.url
    path = u.path or "/"
    if path != "/" and not path.endswith("/"):
        path = path + "/"
    base = urlunparse((u.scheme, u.netloc, path, "", "", ""))
    return {
        "snapshots": [
            urljoin(base, "api/dashboard-snapshot"),
            urljoin(base, "dashboard-snapshot"),
        ],
        "debug_dump": urljoin(base, "api/debug/ac-infinity-dump"),
        "setup": urljoin(base, "setup"),
    }


def _get_credentials() -> tuple[str | None, str | None]:
    """Credentials: wizard file at ENV_FILE_PATH, or OS env only if ACDASH_USE_ENV_CREDENTIALS is set.

    Without that flag, stray ACINFINITY_* in the container environment (Portainer, compose) does not
    skip the setup wizard — matches a \"fresh\" container with no `.env` file.
    """
    if ENV_FILE_PATH.is_file():
        vals = dotenv_values(ENV_FILE_PATH)
        email = (vals.get("ACINFINITY_EMAIL") or "").strip()
        password = (vals.get("ACINFINITY_PASSWORD") or "").strip()
        if email and password:
            return email, password
    flag = (os.getenv("ACDASH_USE_ENV_CREDENTIALS") or "").strip().lower()
    if flag in ("1", "true", "yes"):
        e = (os.getenv("ACINFINITY_EMAIL") or "").strip()
        p = (os.getenv("ACINFINITY_PASSWORD") or "").strip()
        if e and p:
            return e, p
    return None, None


def credentials_configured() -> bool:
    email, password = _get_credentials()
    return bool(email and password)


def _dotenv_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def save_credentials_file(email: str, password: str) -> None:
    lines = [
        "# AC Dash — local credentials. Delete this file to run setup again.",
        f"ACINFINITY_EMAIL={_dotenv_quote(email.strip())}",
        f"ACINFINITY_PASSWORD={_dotenv_quote(password)}",
        "",
    ]
    ENV_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE_PATH.write_text("\n".join(lines), encoding="utf-8")
    try:
        ENV_FILE_PATH.chmod(0o600)
    except OSError:
        pass


def _clear_cache() -> None:
    _cache["at"] = 0.0
    _cache["controllers"] = []
    _cache["error"] = None


def _fetch_controllers() -> tuple[list[dict], str | None]:
    email, password = _get_credentials()
    if not email or not password:
        return [], "Credentials not configured."

    client = ACInfinityClient(email, password)
    try:
        raw = client.get_devices()
    except Exception as exc:
        logger.error("Failed to fetch devices from AC Infinity: %s", exc, exc_info=True)
        return [], f"Could not reach AC Infinity ({type(exc).__name__})."
    finally:
        client.close()

    if not raw:
        return [], "No data from AC Infinity (check credentials or API availability)."

    try:
        return normalize_devices(raw), None
    except Exception as exc:
        logger.error("Failed to normalize device data: %s", exc, exc_info=True)
        return [], "Error processing device data."


def get_cached_controllers() -> tuple[list[dict], str | None, float]:
    now = time.monotonic()
    last = float(_cache["at"])
    if _cache["controllers"] and (now - last) < CACHE_SECONDS:
        return _cache["controllers"], _cache["error"], last  # type: ignore[return-value]

    controllers, err = _fetch_controllers()
    _cache["controllers"] = controllers
    _cache["error"] = err
    _cache["at"] = now
    return controllers, err, now


@app.get("/health")
def health() -> PlainTextResponse:
    return PlainTextResponse("OK", status_code=200)


@app.get("/setup", response_class=HTMLResponse, response_model=None)
def setup_get() -> Union[HTMLResponse, RedirectResponse]:
    if credentials_configured():
        return RedirectResponse("/", status_code=302)
    template = env.get_template("setup.html")
    return HTMLResponse(template.render(error=None))


@app.post("/setup", response_class=HTMLResponse, response_model=None)
def setup_post(
    email: str = Form(...),
    password: str = Form(...),
) -> Union[HTMLResponse, RedirectResponse]:
    if credentials_configured():
        return RedirectResponse("/", status_code=302)

    email = email.strip()
    password = password.strip()
    if not email or not password:
        template = env.get_template("setup.html")
        return HTMLResponse(
            template.render(error="Email and password are required."),
            status_code=400,
        )

    client = ACInfinityClient(email, password)
    try:
        ok = client.authenticate()
    finally:
        client.close()

    if not ok:
        template = env.get_template("setup.html")
        detail = (getattr(client, "last_auth_error", None) or "").strip()
        err_msg = "Could not sign in to AC Infinity."
        if detail:
            err_msg += " " + detail
        else:
            err_msg += " Check email and password."
        return HTMLResponse(
            template.render(error=err_msg),
            status_code=400,
        )

    save_credentials_file(email, password)
    _clear_cache()
    logger.info("Saved credentials to %s", ENV_FILE_PATH)
    return RedirectResponse("/", status_code=303)


@app.get("/api/debug/ac-infinity-dump")
def ac_infinity_debug_dump() -> JSONResponse:
    """Large JSON: full devInfoListAll response, normalized cards data, and per-port settings APIs."""
    if not credentials_configured():
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    email, password = _get_credentials()
    if not email or not password:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    client = ACInfinityClient(email, password)
    try:
        bundle = collect_debug_bundle(client)
    finally:
        client.close()

    note = (
        "Sensitive: your controllers and settings. Share only with people you trust. "
        "Correlate devices_enriched[].devId with devInfoListAll.data[].devId."
    )
    if isinstance(bundle.get("acdash"), dict):
        bundle["acdash"]["note"] = note
    return JSONResponse(
        bundle,
        headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"},
    )


@app.get("/api/dashboard-snapshot")
@app.get("/dashboard-snapshot")
def dashboard_snapshot() -> JSONResponse:
    """Fresh fetch for live UI updates without full page reload."""
    if not credentials_configured():
        return JSONResponse(
            {"error": "Unauthorized", "cards_html": "", "show_empty": False},
            status_code=401,
        )

    controllers, error = _fetch_controllers()
    _cache["controllers"] = controllers
    _cache["error"] = error
    _cache["at"] = time.monotonic()

    cards_html = env.get_template("partials/cards_only.html").render(controllers=controllers)
    show_empty = not controllers and error is None

    controllers_meta = [{"id": c["id"], "name": c["name"]} for c in controllers]
    return JSONResponse(
        {
            "error": error,
            "cards_html": cards_html,
            "show_empty": show_empty,
            "controllers": controllers_meta,
        },
        headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"},
    )


@app.get("/api/controller-stages")
def get_controller_stages() -> JSONResponse:
    """Return all saved stage labels keyed by controller dev_id."""
    if not credentials_configured():
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(storage.get_all_stages())


@app.post("/api/controller-stage")
async def set_controller_stage_endpoint(request: Request) -> JSONResponse:
    """Save a grow stage label for a controller."""
    if not credentials_configured():
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    dev_id = (body.get("dev_id") or "").strip()
    stage = (body.get("stage") or "").strip()
    if not dev_id or not stage:
        return JSONResponse({"error": "dev_id and stage are required"}, status_code=400)
    storage.set_controller_stage(dev_id, stage)
    return JSONResponse({"ok": True})


@app.get("/api/history-chart")
def api_history_chart(
    dev_id: str = Query("", alias="dev_id"),
    hours: float = Query(24.0, ge=1.0, le=720.0),
) -> JSONResponse:
    """Paged ``log/dataPage`` history for Chart.js (dev_id must belong to the signed-in account)."""
    if not credentials_configured():
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    dev_id = (dev_id or "").strip()
    if not dev_id:
        return JSONResponse({"error": "dev_id is required"}, status_code=400)

    cached = _get_history_cache(dev_id, hours)
    if cached is not None:
        logger.debug("History cache hit: dev_id=%s hours=%s", dev_id, hours)
        return JSONResponse(cached, headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"})

    # SQLite-first: serve from local DB when we have adequate coverage
    now_ts = int(time.time())
    start_ts = now_ts - int(hours * 3600)
    local_count = storage.count_readings(dev_id, start_ts, now_ts)
    expected = (hours * 3600) / max(COLLECTOR_INTERVAL, 1)
    if local_count >= max(10, int(expected * 0.4)):
        local_points = storage.query_readings(dev_id, start_ts, now_ts)
        thinned = thin_points(local_points, 1200)
        span_secs = (thinned[-1]["t"] - thinned[0]["t"]) if len(thinned) >= 2 else 0
        local_meta = {
            "source": "local",
            "points": len(thinned),
            "points_unthinned": local_count,
            "hours_requested": hours,
            "span_hours_rounded": round(span_secs / 3600, 1),
            "window_hours_rounded": round(hours, 1),
            "pages_fetched": 0,
            "api_total_max": 0,
        }
        result = {"points": thinned, "meta": local_meta}
        _set_history_cache(dev_id, hours, result)
        logger.debug("History served from local DB: dev_id=%s count=%d", dev_id, local_count)
        return JSONResponse(result, headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"})

    email, password = _get_credentials()
    if not email or not password:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    client = ACInfinityClient(email, password)
    try:
        raw = client.get_devices()
        allowed = {str(d.get("devId")) for d in raw if d.get("devId")}
        if dev_id not in allowed:
            return JSONResponse({"error": "Controller not found on this account"}, status_code=404)

        def fetch_page(
            d: str,
            time_end: int,
            time_start: int,
            page_size: int,
            *,
            order_direction: int = 1,
        ) -> dict[str, Any]:
            return (
                client.history_data_page(
                    d,
                    time_end,
                    time_start,
                    page_size=page_size,
                    order_direction=order_direction,
                )
                or {}
            )

        points, meta = fetch_history_for_chart(
            history_page_fn=fetch_page,
            dev_id=dev_id,
            hours=float(hours),
        )
    finally:
        client.close()

    result = {"points": points, "meta": meta}
    _set_history_cache(dev_id, hours, result)
    return JSONResponse(result, headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"})


@app.get("/", response_class=HTMLResponse, response_model=None)
def dashboard(request: Request) -> Union[HTMLResponse, RedirectResponse]:
    if not credentials_configured():
        return RedirectResponse("/setup", status_code=302)

    controllers, error, _ = get_cached_controllers()
    template = env.get_template("dashboard.html")
    controllers_meta = [{"id": c["id"], "name": c["name"]} for c in controllers]
    html = template.render(
        controllers=controllers,
        controllers_json=json.dumps(controllers_meta),
        error=error,
        client_urls_json=json.dumps(_client_dashboard_urls(request)),
    )
    return HTMLResponse(html)
