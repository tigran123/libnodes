"""Where in the library this browser was, so the rail's Library link goes back there.

The position lives only in `?p=`, and the rail's links are bare, so a cookie carries it.
It fills in the *link* and nothing else: a bare `/library`, a bookmark and Back keep
meaning what they say. The directory only -- not `q` or `sort`.
"""

from __future__ import annotations

from urllib.parse import quote, unquote

from fastapi import Request
from fastapi.responses import Response

from .library import LibraryIndex, PathError, normalise

POS_COOKIE = "libnodes_lib_path"

#: A year, matching `libnodes_view` (routes/devices.py), `libnodes_card_hide` and the theme
#: cookie (app.js): they are all facts about one browser and none is worth asking twice.
POS_MAX_AGE = 31536000


def remember(response: Response, path: str) -> None:
    """Record the directory a library page is showing, percent-encoded: a path with a
    comma, a space or Cyrillic is not a cookie value. The root is recorded too, as ""."""
    response.set_cookie(
        POS_COOKIE,
        quote(path, safe=""),
        # No `secure` on plain http; httponly, as only the server reads it.
        httponly=True,
        samesite="lax",
        path="/",
        max_age=POS_MAX_AGE,
    )


def resolved_pos(request: Request, index: LibraryIndex) -> str:
    """The remembered directory, or "" for the root. Validated against the index on every
    read -- a path can go stale or be hand-edited to `.data` -- and forgotten rather than
    raised, which would break every page's rail."""
    raw = request.cookies.get(POS_COOKIE)
    if not raw:
        return ""
    try:
        path = normalise(unquote(raw))
    except PathError:
        return ""
    if not path:
        return ""
    entry = index.entry(path)
    if entry is None or not entry.is_dir:
        return ""
    return path


def library_href(path: str) -> str:
    """The rail's Library link for a remembered position."""
    return f"/library?p={quote(path, safe='')}" if path else "/library"


__all__ = ["POS_COOKIE", "POS_MAX_AGE", "library_href", "remember", "resolved_pos"]
