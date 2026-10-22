from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..adapters.base import AuthExpired, Token, pkce_pair
from ..adapters.simkl import ISSUER as SIMKL_ISSUER
from ..config import Settings
from ..domain import Action, Kind
from ..runtime import Runtime

log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
SERVICES = ("trakt", "simkl")


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime = Runtime(settings or Settings.from_env())
    store = runtime.store

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.create_task(runtime.loop())
        yield
        loop.cancel()

    app = FastAPI(lifespan=lifespan)

    def back(path: str = "/") -> RedirectResponse:
        return RedirectResponse(path, status_code=303)

    def render(request: Request, name: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"busy": runtime.busy, **context})

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        accounts = [_account_view(store.account(s), s in runtime.oauth) for s in SERVICES]
        runs = store.runs(10)
        awaiting = next((r for r in runs if r["status"] == "dry-run"), None)
        needs_approval = any(a["connected"] and a["needs_dry_run"] for a in accounts)
        return render(
            request,
            "index.html",
            accounts=accounts,
            runs=runs,
            awaiting=awaiting,
            needs_approval=needs_approval,
            settings=runtime.settings,
            unmatched=sum(1 for r in store.unmatched() if r["state"] == "open"),
            conflicts=sum(1 for r in store.conflicts() if r["resolution"] is None),
        )

    @app.post("/sync")
    async def sync_now(dry_run: bool = Form(False)):
        runtime.spawn(runtime.sync(dry_run=dry_run))
        return back()

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: int):
        run = store.run(run_id)
        if run is None:
            raise HTTPException(404)
        plan = json.loads(run["plan_json"]) if run["plan_json"] else {"ops": [], "conflicts": [], "skips": []}
        return render(request, "run.html", run=run, plan=plan)

    @app.post("/runs/{run_id}/approve")
    async def approve(run_id: int):
        runtime.spawn(runtime.approve(run_id))
        return back()

    @app.get("/connect/{service}")
    def connect(service: str):
        oauth = runtime.oauth.get(service)
        if oauth is None:
            raise HTTPException(400, f"{service} client ID is not configured")
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        store.put_oauth_state(state, service, verifier)
        return RedirectResponse(oauth.authorize_url(state, challenge), status_code=302)

    @app.get("/oauth/{service}/callback", response_class=HTMLResponse)
    async def callback(request: Request, service: str, state: str = "", code: str = "", error: str = "", iss: str = ""):
        oauth = runtime.oauth.get(service)
        verifier = store.pop_oauth_state(state, service) if state else None
        if oauth is None or verifier is None:
            raise HTTPException(400, "Unknown or expired login attempt. Start again from the dashboard.")
        if service == "simkl" and iss != SIMKL_ISSUER:
            raise HTTPException(400, "Login response did not come from Simkl.")
        if error or not code:
            return render(request, "message.html", title="Login cancelled", message=error or "No code returned.")
        try:
            token = await oauth.exchange(code, verifier)
        except AuthExpired as exc:
            return render(request, "message.html", title="Login failed", message=str(exc))
        store.connect(service, token.access_token, token.refresh_token, token.expires_at, token.scope)
        return back()

    @app.post("/disconnect/{service}")
    async def disconnect(service: str):
        account = store.account(service)
        oauth = runtime.oauth.get(service)
        if oauth and account.connected:
            try:
                await oauth.revoke(Token(account.access_token, account.refresh_token, account.expires_at or "", account.scope))
            except Exception:
                log.warning("revoking %s token failed", service, exc_info=True)
        store.disconnect(service)
        return back()

    @app.get("/unmatched", response_class=HTMLResponse)
    def unmatched(request: Request):
        rows = store.unmatched()
        titles = store.titles([r["title_id"] for r in rows])
        items = [
            {"row": r, "title": titles[r["title_id"]], "candidates": json.loads(r["candidates_json"])} for r in rows
        ]
        return render(request, "unmatched.html", items=items)

    @app.post("/unmatched/{service}/{title_id}/link")
    def link(service: str, title_id: int, namespace: str = Form(...), value: str = Form(...), kind: str = Form(...)):
        value = value.strip()
        if not value:
            raise HTTPException(400, "Enter an ID")
        survivor = store.attach_ids(title_id, Kind(kind), {namespace: value}, source="manual")
        store.clear_unmatched(service, survivor)
        return back("/unmatched")

    @app.post("/unmatched/{service}/{title_id}/link-candidate")
    def link_candidate(service: str, title_id: int, index: int = Form(...)):
        row = next((r for r in store.unmatched() if r["service"] == service and r["title_id"] == title_id), None)
        if row is None:
            raise HTTPException(404)
        candidate = json.loads(row["candidates_json"])[index]
        survivor = store.attach_ids(title_id, Kind(candidate["type"]), candidate["ids"], source="manual")
        store.clear_unmatched(service, survivor)
        return back("/unmatched")

    @app.post("/unmatched/{service}/{title_id}/{state}")
    def set_unmatched(service: str, title_id: int, state: str):
        if state == "retry":
            store.clear_unmatched(service, title_id)
        elif state in ("ignored", "open"):
            store.set_unmatched_state(service, title_id, state)
        else:
            raise HTTPException(400)
        return back("/unmatched")

    @app.get("/conflicts", response_class=HTMLResponse)
    def conflicts(request: Request):
        rows = store.conflicts()
        titles = store.titles([r["title_id"] for r in rows])
        return render(request, "conflicts.html", items=[{"row": r, "title": titles[r["title_id"]]} for r in rows])

    @app.post("/conflicts/{title_id}/{resolution}")
    def resolve_conflict(title_id: int, resolution: str):
        store.resolve_conflict(title_id, Action(resolution))
        return back("/conflicts")

    @app.get("/watchlist", response_class=HTMLResponse)
    def watchlist(request: Request):
        presence: dict[int, dict[str, str]] = {}
        for s in SERVICES:
            for r in store.remote_items(s):
                presence.setdefault(r["title_id"], {})[s] = r["status"]
        for r in store.unmatched():
            presence.setdefault(r["title_id"], {}).setdefault(r["service"], "unmatched")
        on_a_watchlist = {t: p for t, p in presence.items() if "watchlist" in p.values() or "unmatched" in p.values()}
        titles = store.titles(sorted(on_a_watchlist))
        rows = sorted(
            ({"title": titles[t], "presence": p} for t, p in on_a_watchlist.items()),
            key=lambda r: (r["title"].title or "").casefold(),
        )
        return render(request, "watchlist.html", rows=rows, services=SERVICES)

    @app.get("/log", response_class=HTMLResponse)
    def change_log(request: Request):
        return render(request, "log.html", rows=store.change_log(500))

    return app


def _account_view(account, configured: bool) -> dict:
    expiry = datetime.fromisoformat(account.expires_at) if account.expires_at else None
    if account.auth_error:
        warning = f"Login needs renewing: {account.auth_error}"
    elif account.connected and expiry and expiry < datetime.now(UTC) + timedelta(days=1):
        warning = "Access token expires within a day; it will be refreshed on the next sync."
    else:
        warning = None
    return {
        "service": account.service,
        "configured": configured,
        "connected": account.connected,
        "expires_at": account.expires_at,
        "warning": warning,
        "needs_dry_run": account.needs_dry_run,
        "last_sync_at": account.last_sync_at,
        "last_sync_status": account.last_sync_status,
        "last_error": account.last_error,
    }
