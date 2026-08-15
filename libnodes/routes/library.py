"""Library Explorer: cached tree, instant filter, per-row push targets."""

from __future__ import annotations

import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from ..deps import base_context, state
from ..library import SORTS, Entry, normalise
from ..state import AppState
from ..templating import templates

router = APIRouter()

def library_context(
    request: Request,
    p: str = "",
    q: str = "",
    fmt: list[str] | None = None,
    sort: str = "name",
) -> dict:
    app = state(request)
    started = time.perf_counter()

    entry = app.index.require(p)
    path = entry.path
    fmts = [f for f in (fmt or []) if f]
    sort = sort if sort in SORTS else "name"

    rows = app.index.children(path, q=q or None, fmts=fmts or None, sort=sort)
    total_files, total_bytes = app.index.child_count(path)

    device_ids = [d.id for d in app.devices.config.devices]
    presence = app.manifests.presence(rows, device_ids)

    elapsed_ms = (time.perf_counter() - started) * 1000
    meta = app.index.meta()

    ctx = base_context(request, "library")
    ctx.update(
        {
            "entry": entry,
            "path": path,
            "q": q,
            "fmt": fmts,
            "sort": sort,
            "rows": rows,
            "presence": presence,
            "total_files": total_files,
            "total_bytes": total_bytes,
            "match_count": len(rows),
            "elapsed_ms": elapsed_ms,
            "oob": False,
            "index_meta": meta,
            "tree": app.index.expanded_tree(path),
            "ancestors": app.index.ancestors(path),
            "by_id": app.devices.config.by_id,
            "push_devices": app.devices.config.devices[:2],
            "more_devices": app.devices.config.devices[2:],
        }
    )
    return ctx


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    p: str = "",
    q: str = "",
    fmt: list[str] = Query(default=[]),
    sort: str = "name",
):
    return templates.TemplateResponse(
        request, "library.html", library_context(request, p, q, fmt, sort)
    )


@router.get("/lib/pane", response_class=HTMLResponse)
async def lib_pane(
    request: Request,
    p: str = "",
    q: str = "",
    fmt: list[str] = Query(default=[]),
    sort: str = "name",
):
    """Tree + content together — a tree click changes both."""
    ctx = library_context(request, p, q, fmt, sort)
    return templates.TemplateResponse(request, "lib_pane.html", ctx)


@router.get("/lib/list", response_class=HTMLResponse)
async def lib_list(
    request: Request,
    p: str = "",
    q: str = "",
    fmt: list[str] = Query(default=[]),
    sort: str = "name",
):
    """File-table body plus an out-of-band refresh of the result counter."""
    ctx = library_context(request, p, q, fmt, sort)
    ctx["oob"] = True
    return templates.TemplateResponse(request, "file_rows.html", ctx)


@router.get("/lib/tree", response_class=HTMLResponse)
async def lib_tree(request: Request, p: str = ""):
    ctx = library_context(request, p)
    return templates.TemplateResponse(request, "tree.html", ctx)


@router.get("/lib/tree/children", response_class=HTMLResponse)
async def lib_tree_children(request: Request, p: str = "", depth: int = 0):
    """One level of subdirectories, for a disclosure the tree has not opened before.

    Expanding is the only half of the toggle that needs the server: collapsing hides
    rows that are already in the DOM.
    """
    app = state(request)
    entry = app.index.require(p)
    rows = [
        (child, depth + 1, False)
        for child in app.index.children(entry.path, dirs_only=True, limit=500)
    ]
    ctx = base_context(request, "library")
    ctx.update({"rows": rows, "path": ""})
    return templates.TemplateResponse(request, "tree_rows.html", ctx)


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


@router.post("/lib/reindex", response_class=HTMLResponse)
async def reindex(request: Request):
    app: AppState = state(request)
    app.reindex_soon()
    ctx = base_context(request, "library")
    ctx["index_meta"] = app.index.meta()
    return templates.TemplateResponse(request, "fragments/reindexing.html", ctx)


@router.get("/lib/index-status", response_class=HTMLResponse)
async def index_status(request: Request):
    app = state(request)
    ctx = base_context(request, "library")
    ctx["index_meta"] = app.index.meta()
    return templates.TemplateResponse(request, "fragments/index_status.html", ctx)
