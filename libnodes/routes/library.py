"""Library Explorer: one panel — breadcrumb, instant filter, per-row push targets."""

from __future__ import annotations

import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from ..deps import base_context, state
from ..libpos import library_href, remember
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
    selectable = [d for d in app.devices.config.devices if d.is_selectable]

    elapsed_ms = (time.perf_counter() - started) * 1000
    meta = app.index.meta()

    ctx = base_context(request, "library")
    # The rail's own Library link, overwritten because base_context read it from the
    # cookie and the cookie is one navigation behind: on /library?p=A it still holds
    # wherever you were before. A rail link pointing somewhere other than the page you
    # are looking at is the "the URL moved and the content did not" failure again.
    ctx["library_href"] = library_href(path)
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
            "ancestors": app.index.ancestors(path),
            "by_id": app.devices.config.by_id,
            # Mirror nodes are not selection targets. `_resolve` cannot offer them .data/,
            # so a subtree of preserved symlinks would land pointing at a vault that is not
            # there — the dangling-link failure, from the other direction. They replicate
            # the whole root or nothing. They stay in `presence` above: what a mirror holds
            # is worth showing, it just is not pushed to from here.
            "push_devices": selectable[:2],
            "more_devices": selectable[2:],
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
    """The one full page. It records where it is, so the rail can come back here.

    `ctx["path"]` and not `p`: that is the normalised path `index.require` has already
    vouched for, so nothing a query string can say reaches the cookie unchecked.
    """
    ctx = library_context(request, p, q, fmt, sort)
    response = templates.TemplateResponse(request, "library.html", ctx)
    remember(response, ctx["path"])
    return response


@router.get("/lib/pane", response_class=HTMLResponse)
async def lib_pane(
    request: Request,
    p: str = "",
    q: str = "",
    fmt: list[str] = Query(default=[]),
    sort: str = "name",
):
    """The whole panel. A breadcrumb segment and a directory name both swap it, so the
    listing, the crumb and the selection change together.

    It records the position too, and it is the one that matters: walking the tree never
    reloads the page, so without this the cookie would only ever hold where you *arrived*.
    A bare call records the root, which is right rather than the `/devices` hazard
    restated -- that rule is that arriving by the rail must not pin a default the handler
    merely guessed, and there is no guess here: a bare /lib/pane is the breadcrumb's root
    link, and the root is then where you are.

    /lib/list and /lib/selection deliberately do not record anything. Filtering, sorting
    and ticking a box do not move you, and the filter fires on every keystroke.
    """
    ctx = library_context(request, p, q, fmt, sort)
    response = templates.TemplateResponse(request, "lib_pane.html", ctx)
    remember(response, ctx["path"])
    return response


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
