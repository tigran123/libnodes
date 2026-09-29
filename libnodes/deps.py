"""Shared helpers for the route modules: state access and base template context."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request

from .cardprefs import resolved_cards
from .host import host_stats
from .libpos import library_href, resolved_pos
from .state import AppState


def state(request: Request) -> AppState:
    return request.app.state.lib


def base_context(request: Request, active: str) -> dict:
    """Everything `base.html` needs, for both full pages and dock-bearing fragments.

    Includes the dock's own context, because `base.html` embeds `dock_shell.html`, and
    that template decides whether this page opens an SSE connection at all.
    """
    app = state(request)
    running, pending = app.jobs.counts()
    index_meta = app.index.meta()
    ctx = {
        "request": request,
        "active": active,
        # The library's own size from the index; statvfs would count the whole disk.
        "library_bytes": index_meta.total_bytes,
        "index_meta": index_meta,
        "theme": "light" if request.cookies.get("libnodes_theme") == "light" else "dark",
        "auth_enabled": app.settings.auth_enabled,
        "job_count": running + pending,
        "host": host_stats(app.settings.library_root),
        "library_root": str(app.settings.library_root),
        "settings": app.settings,
        "devices": app.devices.config.devices,
        "by_id": app.devices.config.by_id,
        # Here, because a card renders from six places, some with only this context.
        "card_show": resolved_cards(request),
        # The rail's Library link; `library_context` overwrites it with the page's own.
        "library_href": library_href(resolved_pos(request, app.index)),
    }
    # Under `dock`, or the Jobs page's own `jobs` would clobber it. Imported here because
    # routes.jobs imports this module.
    from .routes.jobs import dock_context

    ctx["dock"] = dock_context(app)
    return ctx


def short_path(path: str, keep: int = 2) -> str:
    """`…/Computing/Kernel & Drivers` — for tab strips and toasts."""
    parts = [p for p in Path(path).parts if p not in ("/", "")]
    if len(parts) <= keep:
        return "/".join(parts) or "(library)"
    return "…/" + "/".join(parts[-keep:])


__all__ = ["base_context", "short_path", "state"]
