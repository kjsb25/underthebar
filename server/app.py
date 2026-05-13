"""FastAPI app for the always-on Strava → Hevy import service.

Routes group:
  /              dashboard (status, recent imports, manual import picker)
  /settings      filters, private toggle, polling interval, creds
  /auth          Strava OAuth bootstrap + Hevy token paste
  /api/*         JSON endpoints used by the dashboard
  /healthz       liveness
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urlencode

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from hevy_client import HevyClient, HevyError
from poller import Poller
from state import build_state, State
from strava_client import (
    ALL_ACTIVITY_TYPES,
    StravaClient,
    StravaError,
    VIRTUAL_RIDE_TYPE,
    VIRTUAL_RIDE_OWNER_HEVY_USER_ID,
    test_credentials,
)


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    state: State = build_state()
    strava = StravaClient(state)
    hevy = HevyClient(state)
    poller = Poller(state, strava, hevy)
    app.state.state = state
    app.state.strava = strava
    app.state.hevy = hevy
    app.state.poller = poller
    poller.start()
    state.log("INFO", "Service started")
    try:
        yield
    finally:
        await poller.stop()
        state.log("INFO", "Service stopping")


app = FastAPI(title="Strava → Hevy Import Service", lifespan=lifespan)


# ── Helpers ───────────────────────────────────────────────────────────────
def _ctx(request: Request, **extra) -> dict:
    s: State = request.app.state.state
    hevy: HevyClient = request.app.state.hevy
    strava: StravaClient = request.app.state.strava
    enabled = set(s.get_json("enabled_types", []))
    types = [
        {"type": at.type, "title": at.title, "enabled": at.type in enabled}
        for at in ALL_ACTIVITY_TYPES
    ]
    if hevy.state.get("hevy_user_id") == VIRTUAL_RIDE_OWNER_HEVY_USER_ID:
        types.append(
            {
                "type": VIRTUAL_RIDE_TYPE.type,
                "title": VIRTUAL_RIDE_TYPE.title,
                "enabled": VIRTUAL_RIDE_TYPE.type in enabled,
            }
        )
    base = {
        "request": request,
        "title": "Strava → Hevy",
        "strava_authorized": strava.is_authorized(),
        "strava_has_creds": strava.has_credentials(),
        "hevy_authorized": hevy.is_authorized(),
        "hevy_username": s.get("hevy_username") or "",
        "strava_client_id": s.get("strava_client_id") or "",
        "strava_client_secret_set": bool(s.get("strava_client_secret")),
        "enabled_types": types,
        "import_private": s.get_bool("import_private"),
        "polling_enabled": s.get_bool("polling_enabled"),
        "poll_interval_seconds": s.get_int("poll_interval_seconds", 600),
        "import_lookback_hours": s.get_int("import_lookback_hours", 24),
        "last_poll_at": s.get("last_poll_at") or "never",
        "counts": s.import_counts(),
    }
    base.update(extra)
    return base


def _strava_redirect_uri(request: Request) -> str:
    override = os.environ.get("PUBLIC_BASE_URL")
    base = override.rstrip("/") if override else str(request.base_url).rstrip("/")
    return f"{base}/auth/strava/callback"


# ── Pages ─────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    s: State = request.app.state.state
    ctx = _ctx(
        request,
        recent_imports=s.recent_imports(20),
        recent_logs=s.recent_logs(50),
    )
    return templates.TemplateResponse("dashboard.html", ctx)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str | None = None):
    return templates.TemplateResponse(
        "settings.html", _ctx(request, saved=saved)
    )


@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request, msg: str | None = None, error: str | None = None):
    return templates.TemplateResponse(
        "auth.html", _ctx(request, msg=msg, error=error)
    )


# ── Settings POSTs (form-encoded) ─────────────────────────────────────────
@app.post("/settings/strava-creds")
async def save_strava_creds(
    request: Request,
    client_id: str = Form(""),
    client_secret: str = Form(""),
):
    s: State = request.app.state.state
    updates: dict[str, str | None] = {}
    cid = client_id.strip()
    if cid:
        updates["strava_client_id"] = cid
    csec = client_secret.strip()
    if csec:
        updates["strava_client_secret"] = csec
    if updates:
        s.set_many(updates)
        s.log("INFO", "Strava credentials updated")
    return RedirectResponse("/settings?saved=strava-creds", status_code=303)


@app.post("/settings/strava-creds/test")
async def test_strava_creds(request: Request):
    s: State = request.app.state.state
    ok, msg = test_credentials(
        s.get("strava_client_id") or "", s.get("strava_client_secret") or ""
    )
    return JSONResponse({"ok": ok, "message": msg})


@app.post("/settings/types")
async def save_types(request: Request):
    """Activity-type filter checkboxes — mirrors save_strava_type_filters()."""
    form = await request.form()
    selected = [k for k in form.keys() if k.startswith("type_")]
    type_names = [k[len("type_") :] for k in selected]
    valid_types = {at.type for at in ALL_ACTIVITY_TYPES} | {VIRTUAL_RIDE_TYPE.type}
    type_names = [t for t in type_names if t in valid_types]
    s: State = request.app.state.state
    s.set_json("enabled_types", type_names)
    s.log("INFO", f"Activity types set to {type_names}")
    return RedirectResponse("/settings?saved=types", status_code=303)


@app.post("/settings/private")
async def save_private(request: Request, import_private: str = Form("")):
    s: State = request.app.state.state
    s.set_bool("import_private", bool(import_private))
    s.log("INFO", f"import_private set to {bool(import_private)}")
    return RedirectResponse("/settings?saved=private", status_code=303)


@app.post("/settings/polling")
async def save_polling(
    request: Request,
    polling_enabled: str = Form(""),
    poll_interval_seconds: int = Form(600),
    import_lookback_hours: int = Form(24),
):
    s: State = request.app.state.state
    s.set_bool("polling_enabled", bool(polling_enabled))
    s.set("poll_interval_seconds", str(max(60, poll_interval_seconds)))
    s.set("import_lookback_hours", str(max(1, import_lookback_hours)))
    s.log(
        "INFO",
        f"Polling enabled={bool(polling_enabled)} interval={poll_interval_seconds}s lookback={import_lookback_hours}h",
    )
    request.app.state.poller.kick()
    return RedirectResponse("/settings?saved=polling", status_code=303)


# ── Auth ──────────────────────────────────────────────────────────────────
@app.get("/auth/strava")
async def strava_auth_start(request: Request):
    strava: StravaClient = request.app.state.strava
    if not strava.has_credentials():
        return RedirectResponse(
            "/auth?error=Set+Strava+client+ID%2Fsecret+first", status_code=303
        )
    try:
        url = strava.authorization_url(_strava_redirect_uri(request))
    except StravaError as e:
        return RedirectResponse(
            f"/auth?error={_urlquote(str(e))}", status_code=303
        )
    return RedirectResponse(url, status_code=303)


@app.get("/auth/strava/callback")
async def strava_auth_callback(
    request: Request,
    code: str | None = None,
    error: str | None = None,
):
    if error:
        return RedirectResponse(
            f"/auth?error={_urlquote(error)}", status_code=303
        )
    if not code:
        return RedirectResponse(
            "/auth?error=Missing+code+parameter", status_code=303
        )
    strava: StravaClient = request.app.state.strava
    try:
        strava.exchange_code(code)
    except StravaError as e:
        return RedirectResponse(
            f"/auth?error={_urlquote(str(e))}", status_code=303
        )
    return RedirectResponse("/auth?msg=Strava+connected", status_code=303)


@app.post("/auth/hevy")
async def save_hevy_tokens(
    request: Request,
    access_token: str = Form(""),
    refresh_token: str = Form(""),
    expires_at: str = Form(""),
    cookie_value: str = Form(""),
):
    access_token = access_token.strip()
    refresh_token = refresh_token.strip()
    expires_at = expires_at.strip()
    cookie_value = cookie_value.strip()

    if cookie_value:
        try:
            parsed = _parse_hevy_cookie(cookie_value)
        except ValueError as e:
            return RedirectResponse(
                f"/auth?error={_urlquote('Could not parse cookie value: ' + str(e))}",
                status_code=303,
            )
        access_token = parsed["access_token"]
        refresh_token = parsed["refresh_token"]
        expires_at = parsed.get("expires_at", "") or expires_at

    if not access_token or not refresh_token:
        return RedirectResponse(
            f"/auth?error={_urlquote('Provide either the auth2.0-token cookie value, or both access and refresh tokens.')}",
            status_code=303,
        )

    hevy: HevyClient = request.app.state.hevy
    hevy.set_tokens(access_token, refresh_token, expires_at or None)
    try:
        hevy.account()  # fetches and caches user_id/username
        msg = "Hevy+tokens+saved+and+verified"
    except HevyError as e:
        return RedirectResponse(
            f"/auth?error={_urlquote('Saved but verify failed: ' + str(e))}",
            status_code=303,
        )
    return RedirectResponse(f"/auth?msg={msg}", status_code=303)


def _parse_hevy_cookie(raw: str) -> dict:
    """Accepts the auth2.0-token cookie value in any of these forms:
      - URL-encoded JSON (what's stored in the cookie itself)
      - Decoded JSON (what DevTools often shows)
      - Either form wrapped in surrounding quotes
    Returns a dict with at least access_token and refresh_token.
    """
    candidate = raw.strip().strip('"').strip("'")
    errors = []
    for attempt in (candidate, unquote(candidate)):
        try:
            data = json.loads(attempt)
        except json.JSONDecodeError as e:
            errors.append(str(e))
            continue
        if not isinstance(data, dict):
            errors.append("cookie value is not a JSON object")
            continue
        if not data.get("access_token") or not data.get("refresh_token"):
            raise ValueError("cookie JSON is missing access_token or refresh_token")
        return data
    raise ValueError("not valid JSON (tried raw and URL-decoded): " + "; ".join(errors))


@app.post("/auth/hevy/clear")
async def clear_hevy(request: Request):
    request.app.state.hevy.clear_tokens()
    return RedirectResponse("/auth?msg=Hevy+tokens+cleared", status_code=303)


# ── Sync / import endpoints ───────────────────────────────────────────────
@app.post("/api/sync")
async def sync_now(request: Request):
    poller: Poller = request.app.state.poller
    result = await poller.poll_once(triggered_by="manual")
    return JSONResponse(result)


@app.get("/api/activities")
async def list_activities(request: Request, limit: int = Query(10, ge=1, le=50)):
    strava: StravaClient = request.app.state.strava
    hevy: HevyClient = request.app.state.hevy
    if not strava.is_authorized():
        raise HTTPException(400, "Strava not authorized")
    try:
        activities = await asyncio.to_thread(
            strava.recent_activities,
            hevy.user_id() if hevy.is_authorized() else None,
            limit,
            request.app.state.state.get_int("import_lookback_hours", 168),
        )
    except StravaError as e:
        raise HTTPException(400, str(e))
    for a in activities:
        a["already_imported"] = request.app.state.state.is_imported(a["id"])
    return JSONResponse({"activities": activities})


@app.post("/api/import/{activity_id}")
async def import_one(request: Request, activity_id: str):
    strava: StravaClient = request.app.state.strava
    hevy: HevyClient = request.app.state.hevy
    state: State = request.app.state.state
    if not (strava.is_authorized() and hevy.is_authorized()):
        raise HTTPException(400, "Not authorized")
    is_private = state.get_bool("import_private")
    try:
        payload, workout_id = await asyncio.to_thread(
            strava.build_hevy_workout, activity_id, hevy.user_id(), is_private
        )
        status = await asyncio.to_thread(hevy.submit_workout, payload, workout_id)
    except (StravaError, HevyError) as e:
        state.log("ERROR", f"Manual import {activity_id} failed: {e}")
        raise HTTPException(502, str(e))
    if status in (200, 201):
        title = payload["workout"]["title"]
        atype = payload["workout"]["exercises"][0]["title"]
        state.mark_imported(activity_id, title, atype, workout_id)
        state.log("INFO", f"Manual import {activity_id} → Hevy {workout_id}")
        return {"ok": True, "status": status, "hevy_workout_id": workout_id}
    state.log("ERROR", f"Manual import {activity_id} HTTP {status}")
    raise HTTPException(502, f"Hevy returned HTTP {status}")


# ── Status / health ───────────────────────────────────────────────────────
@app.get("/api/status")
async def api_status(request: Request):
    s: State = request.app.state.state
    return {
        "strava_authorized": request.app.state.strava.is_authorized(),
        "hevy_authorized": request.app.state.hevy.is_authorized(),
        "hevy_username": s.get("hevy_username"),
        "polling_enabled": s.get_bool("polling_enabled"),
        "poll_interval_seconds": s.get_int("poll_interval_seconds", 600),
        "last_poll_at": s.get("last_poll_at"),
        "counts": s.import_counts(),
    }


@app.get("/healthz")
async def healthz():
    return {"ok": True}


def _urlquote(s: str) -> str:
    return urlencode({"_": s})[2:]
