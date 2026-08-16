"""App factory and route registration."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import AuthMiddleware
from .config import get_devices, get_settings
from .library import PathError
from .routes import auth as auth_routes
from .routes import config_view as config_routes
from .routes import devices as devices_routes
from .routes import jobs as jobs_routes
from .routes import library as library_routes
from .state import AppState

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    settings = get_settings()
    settings.ensure_dirs()
    devices = get_devices()
    lib = AppState(settings, devices)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await lib.start()
        try:
            yield
        finally:
            await lib.stop()

    app = FastAPI(title="LibNodes", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.lib = lib

    # Middleware, not `dependencies=` on the include_router calls below. A router
    # dependency would cover the four route modules and leave /, /healthz, the /fleet
    # and /node redirects and the /static mount open, and every route added later would
    # be a fresh chance to forget one -- the same failure mode as the FRAGMENTS list in
    # tests/test_routes.py. This covers everything by construction; auth.OPEN_PATHS is
    # then the single, readable list of what is deliberately not covered.
    app.add_middleware(AuthMiddleware)

    if not settings.auth_enabled:
        # Fail-open is the deliberate default -- it is what leaves a dev server and the
        # test suite untouched -- so this warning is the only thing standing between a
        # Pi that lost its LIBNODES_PASSWORD and a fleet anyone on the LAN can drive.
        #
        # The app configures no logging at all, so this reaches stderr through
        # logging.lastResort, which has no formatter and prints the bare message. Hence
        # the literal "WARNING:" and the padding: without them the line lands in the
        # journal looking like a stray print, indistinguishable from chatter, next to
        # uvicorn's own "INFO:     " column.
        logging.getLogger("libnodes").warning(
            "WARNING:  no LIBNODES_PASSWORD set - the UI is open to every host that "
            "can reach this port. See deploy/README.md section Access."
        )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(PathError)
    async def _path_error(request: Request, exc: PathError) -> HTMLResponse:
        # Every library path is validated against the index; anything else is a 400,
        # not a traversal.
        return HTMLResponse(
            f'<div class="hint t-err">{exc}</div>', status_code=400
        )

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/devices", status_code=307)

    # --- compatibility with the pre-rename URLs -----------------------------
    #
    # "Fleet" became "Devices". A page loaded before that rename keeps polling
    # /fleet/rows every 10s for as long as it stays open, and without these it just
    # 404s forever: the table silently stops updating and nothing says why. 308
    # preserves the method, so the POST actions redirect correctly too.

    @app.api_route(
        "/fleet", methods=["GET"], include_in_schema=False
    )
    async def _old_fleet() -> RedirectResponse:
        return RedirectResponse("/devices", status_code=308)

    @app.api_route(
        "/fleet/{rest:path}", methods=["GET", "POST"], include_in_schema=False
    )
    async def _old_fleet_sub(rest: str, request: Request) -> RedirectResponse:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/devices/{rest}{query}", status_code=308)

    @app.api_route(
        "/node/{rest:path}",
        methods=["GET", "POST", "PUT", "DELETE"],
        include_in_schema=False,
    )
    async def _old_node(rest: str, request: Request) -> RedirectResponse:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/device/{rest}{query}", status_code=308)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        meta = lib.index.meta()
        running, pending = lib.jobs.counts()
        online, total = lib.probe.reachable_count
        return {
            "ok": True,
            "index": {
                "ready": meta.ready,
                "entries": meta.entry_count,
                "indexed_at": meta.indexed_at,
                "running": meta.running,
            },
            "jobs": {"running": running, "pending": pending},
            "devices": {"online": online, "total": total},
        }

    app.include_router(devices_routes.router)
    app.include_router(library_routes.router)
    app.include_router(jobs_routes.router)
    app.include_router(config_routes.router)
    app.include_router(auth_routes.router)
    return app


app = create_app()
