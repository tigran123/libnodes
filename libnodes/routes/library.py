"""Library Explorer: one panel — breadcrumb, instant filter, per-row push targets."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from ..deps import base_context, state
from ..libpos import library_href, remember
from ..library import SORTS, Entry, normalise
from ..manifests import MAX_COLUMNS, coverage_view, item_view, presence_slots
from ..templating import templates

router = APIRouter()

def library_context(
    request: Request,
    p: str = "",
    q: str = "",
    sort: str = "name",
) -> dict:
    app = state(request)
    started = time.perf_counter()

    entry = app.index.require(p)
    path = entry.path
    sort = sort if sort in SORTS else "name"

    rows = app.index.children(path, q=q or None, sort=sort)
    total_files, total_bytes = app.index.child_count(path)

    fleet = app.devices.config.devices
    device_ids = [d.id for d in fleet]
    presence = app.manifests.presence(rows, device_ids)

    elapsed_ms = (time.perf_counter() - started) * 1000
    meta = app.index.meta()

    ctx = base_context(request, "library")
    # The cookie base_context read is one navigation behind; the rail links here.
    ctx["library_href"] = library_href(path)
    ctx.update(
        {
            "entry": entry,
            "path": path,
            "q": q,
            "sort": sort,
            "rows": rows,
            # Drawn from `slots`, never `presence`: see `presence_slots`.
            "fleet": fleet,
            "slots": presence_slots(presence, device_ids),
            "total_files": total_files,
            "total_bytes": total_bytes,
            "match_count": len(rows),
            "elapsed_ms": elapsed_ms,
            "oob": False,
            "index_meta": meta,
            "ancestors": app.index.ancestors(path),
            "by_id": app.devices.config.by_id,
        }
    )
    return ctx


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    p: str = "",
    q: str = "",
    sort: str = "name",
):
    """The one full page. It records the path the index vouched for, so the rail can
    come back."""
    ctx = library_context(request, p, q, sort)
    response = templates.TemplateResponse(request, "library.html", ctx)
    remember(response, ctx["path"])
    return response


@router.get("/lib/pane", response_class=HTMLResponse)
async def lib_pane(
    request: Request,
    p: str = "",
    q: str = "",
    sort: str = "name",
):
    """The whole panel, swapped by the breadcrumb and by a directory name. It records the
    position, since walking never reloads the page; a bare call is the root link.
    /lib/list and /lib/selection record nothing: filtering does not move you."""
    ctx = library_context(request, p, q, sort)
    response = templates.TemplateResponse(request, "lib_pane.html", ctx)
    remember(response, ctx["path"])
    return response


@router.get("/lib/list", response_class=HTMLResponse)
async def lib_list(
    request: Request,
    p: str = "",
    q: str = "",
    sort: str = "name",
):
    """File-table body plus an out-of-band refresh of the result counter."""
    ctx = library_context(request, p, q, sort)
    ctx["oob"] = True
    return templates.TemplateResponse(request, "file_rows.html", ctx)


@router.get("/lib/selection", response_class=HTMLResponse)
async def lib_selection(
    request: Request,
    path: list[str] = Query(default=[]),
    p: str = "",
):
    app = state(request)
    entries: list[Entry] = []
    for raw in path:
        found = app.index.entry(raw)
        if found is not None:
            entries.append(found)

    total = sum(e.size for e in entries)
    files = sum((e.files or 0) if e.is_dir else 1 for e in entries)

    ctx = base_context(request, "library")
    ctx.update(
        {
            "selected": entries,
            "sel_count": len(entries),
            "sel_bytes": total,
            "sel_files": files,
            "path": normalise(p),
        }
    )
    return templates.TemplateResponse(request, "selection_bar.html", ctx)


@router.get("/lib/presence", response_class=HTMLResponse)
async def presence_dialog(request: Request, p: str = ""):
    """The coverage map: which devices hold this row or directory, how much of it, and
    which of its folders -- in words the fleet's tablets can read without a hover. The
    crumb line opens it for the directory you are in, `/Books` included."""
    app = state(request)
    entry = app.index.require(p)
    fleet = app.devices.config.devices
    device_ids = [d.id for d in fleet]
    scanned = app.manifests.scanned_all(device_ids)

    if entry.is_dir and entry.files:
        folders = [c for c in app.index.children(entry.path) if c.is_dir and c.files]
        too_many = len(folders) if len(folders) > MAX_COLUMNS else 0
        # A thread: the root is every device's whole manifest slice (see `coverage`).
        tallies = await asyncio.to_thread(
            app.manifests.coverage, app.index.db_path, entry.path, device_ids
        )
        view = coverage_view(
            entry, [] if too_many else folders, fleet, tallies, scanned, too_many
        )
    else:
        presence = app.manifests.presence([entry], device_ids)
        slots = presence_slots(presence, device_ids).get(entry.path, [None] * len(fleet))
        view = item_view(entry, fleet, slots, scanned)

    ctx = base_context(request, "library")
    ctx.update(
        {
            "entry": entry,
            "view": view,
            "ancestors": app.index.ancestors(entry.path) if entry.path else [],
        }
    )
    return templates.TemplateResponse(request, "dialogs/presence.html", ctx)


@router.get("/lib/index-status", response_class=HTMLResponse)
async def index_status(request: Request):
    app = state(request)
    ctx = base_context(request, "library")
    ctx["index_meta"] = app.index.meta()
    return templates.TemplateResponse(request, "fragments/index_status.html", ctx)
