"""App factory and route registration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import get_devices, get_settings
from .library import PathError
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
    return app


app = create_app()
