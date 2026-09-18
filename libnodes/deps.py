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
        # The library's own size, summed by the index walk (which stats through the CAS
        # symlinks). statvfs would report the whole filesystem — on pi5 one 917G NVMe holds
        # the library, urantia-library, the work trees and the OS, so it reads 282G used for
        # a 248G library, and the error is in whichever direction the rest of the disk moves.
        "library_bytes": index_meta.total_bytes,
        "index_meta": index_meta,
        "theme": "light" if request.cookies.get("libnodes_theme") == "light" else "dark",
        # Drives the Log out control in base.html. Without it the topbar would offer to
        # log out of a session that does not exist on an unlocked dev server.
        "auth_enabled": app.settings.auth_enabled,
        "job_count": running + pending,
        "host": host_stats(app.settings.library_root),
        "library_root": str(app.settings.library_root),
        "settings": app.settings,
        "devices": app.devices.config.devices,
        # Beside `devices` because every toast needs it: fragments/queued.html looks a
        # job's device up by id, and without this it fell back to the raw yaml id — a
        # dry run to "OLD LG G4 (Android 6)" announced itself as `lg2`.
        "by_id": app.devices.config.by_id,
        # Which parts of a GRID card to draw. Here and not in `devices_context` because
        # the card renders from six places and that function covers three: /device/{id}/card
        # and the Test dialog's out-of-band include build their context from base_context
        # alone, and the second of those is the swap that fails *silently* when a name is
        # missing -- the failure already recorded against #device-rows in CLAUDE.md.
        "card_show": resolved_cards(request),
        # Where the rail's Library link goes. Here rather than in the library's own
        # context because the rail is drawn on every page *except* the one that knows:
        # the whole point is to get back from Devices and Jobs. `library_context`
        # overwrites it with the directory actually on screen -- see the note there.
        "library_href": library_href(resolved_pos(request, app.index)),
    }
    # Namespaced under `dock` rather than merged: the Jobs page has its own `jobs`
    # variable (the whole history table), which would otherwise clobber the dock's
    # (only the live cards) and make every page open a stream.
    # Imported here rather than at module scope: routes.jobs imports this module.
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
