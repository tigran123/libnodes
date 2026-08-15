"""The devices.yaml view, plus placeholders for the Stage 2 nav destinations."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sse_starlette.sse import EventSourceResponse

from .. import yamlview
from ..deps import base_context, state
from ..templating import templates

router = APIRouter()


def devices_context(request: Request) -> dict:
    app = state(request)
    store = app.devices
    store.reload(force=True)
    issues = store.issues
    bad_lines = {i.line for i in issues if i.line}
    # "config", not "devices": the two are separate nav items, and sharing a key lights
    # both of them up at once.
    ctx = base_context(request, "config")
    ctx.update(
        {
            "path": str(store.path),
            "mtime": store.mtime,
            "raw": store.text,
            "lines": yamlview.highlight(store.text, bad_lines),
            "issues": issues,
            "node_count": len(store.config.devices),
        }
    )
    return ctx


@router.get("/devices.yaml", response_class=HTMLResponse)
async def devices_page(request: Request):
    return templates.TemplateResponse(request, "devices_yaml.html", devices_context(request))


@router.get("/devices.yaml/stream")
async def config_stream(request: Request):
    """Push the re-rendered panel whenever the file changes on disk.

    Driven by inotify, so an edit appears as fast as the kernel can report it and an
    untouched file costs nothing at all -- no interval, no request per tick.
    """
    app = state(request)
    watcher = app.config_watch
    queue = watcher.subscribe()

    async def publisher():
        try:
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    continue  # EventSourceResponse sends its own keep-alives
                # Coalesce the burst one save produces (write, chmod, rename).
                await asyncio.sleep(0.05)
                while not queue.empty():
                    queue.get_nowait()
                app.devices.reload(force=True)
                yield {"event": "config", "data": render_panel(request), "retry": 3000}
        finally:
            watcher.unsubscribe(queue)

    return EventSourceResponse(publisher(), ping=15)


def render_panel(request: Request) -> str:
    """The panel as a bare string, for the SSE payload."""
    return templates.env.get_template("devices_panel.html").render(
        **devices_context(request)
    )


@router.get("/devices.yaml/panel", response_class=HTMLResponse)
async def panel(request: Request):
    """Re-read the file from disk and re-render the whole panel.

    Both Reload and Validate land here. Returning only the validation strip — as this
    used to — means a *valid* file produces an empty fragment, so the button appears to
    do nothing whatsoever.
    """
    return templates.TemplateResponse(request, "devices_panel.html", devices_context(request))


@router.post("/devices.yaml/validate", response_class=HTMLResponse)
async def validate(request: Request):
    """Validation strip alone, for callers that only want the error band."""
    return templates.TemplateResponse(
        request, "fragments/validation_strip.html", devices_context(request)
    )


@router.get("/devices.yaml/raw", response_class=PlainTextResponse)
async def raw(request: Request):
    store = state(request).devices
    store.reload(force=True)
    return PlainTextResponse(
        store.text,
        headers={"Content-Disposition": 'attachment; filename="devices.yaml"'},
    )


def _stub(request: Request, active: str, title: str, note: str) -> HTMLResponse:
    ctx = base_context(request, active)
    ctx.update({"stub_title": title, "stub_note": note})
    return templates.TemplateResponse(request, "stub.html", ctx)


@router.get("/presets", response_class=HTMLResponse)
async def presets(request: Request):
    return _stub(
        request,
        "presets",
        "Presets",
        "Saved selections you can re-push in one action. Planned for stage 2.",
    )


@router.get("/keys", response_class=HTMLResponse)
async def keys(request: Request):
    return _stub(
        request,
        "keys",
        "Keys",
        "SSH identities live in the service user's ~/.ssh. LibNodes never handles "
        "passwords — BatchMode=yes makes a missing key fail fast instead of hanging.",
    )


@router.get("/device/new", response_class=HTMLResponse)
async def device_new(request: Request):
    return _stub(
        request,
        "devices",
        "Add device",
        "The device configuration drawer is stage 2. For now, edit devices.yaml directly "
        "on the Pi — the Devices view reloads it whenever its mtime changes.",
    )
