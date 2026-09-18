"""Where in the library this browser was, so the rail can take it back there.

A leaf module beside `cardprefs.py` and for the same reason: `deps.py` reads it on every
render, and `deps.py` is what every route module imports.

The rail links in `base.html` are plain `href`s — a full browser navigation with no query
string — while the Library's position lives entirely in `?p=`, pushed into the address bar
by htmx. So walking to Devices and clicking Library went back to /Books every time. That is
the TABLE/GRID bug restated: "the toggle is a link, so the choice lived only in the query
string, and base.html's rail points at a bare /devices". A cookie carries it, a year long,
beside `libnodes_view` and `libnodes_card_hide`.

What this does **not** do is reinterpret a bare `/library`. The cookie fills in the *rail
link* (`/library?p=Fiction%2FLeonid-Perov`) and nothing else, so a typed URL, a bookmark and
the Back button all go on meaning exactly what they say, and there is no state in which the
address bar and the listing disagree.

The directory and nothing else: `q`, `fmt` and `sort` are not carried, which is what
entering a directory already does — the row link carries `p` alone and the pane comes back
with an empty filter box.
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
    """Record the directory a library page is showing.

    Percent-encoded, and that is load-bearing: a library path is not a cookie value. A
    comma or a space makes Python's http.cookies quote and escape the whole thing — the bug
    `cardprefs.SEP` documents, where `addr.target.seen` went out as `"addr\\054target\\054seen"`
    and came back unsplittable — and this library holds Cyrillic directory names besides.
    `quote`/`unquote` sidesteps the class rather than dodging one character of it.

    The root is written as the empty value rather than deleted, so there is one code path
    and "I am at the root" is a recorded position like any other. Without that the
    breadcrumb's root link would be the one navigation the memory ignored.
    """
    response.set_cookie(
        POS_COOKIE,
        quote(path, safe=""),
        # No `secure`: LibNodes is served over plain http on the LAN, so a Secure cookie
        # would never be stored. httponly because nothing on the client reads this one —
        # the rail is rendered server-side, which is the whole point.
        httponly=True,
        samesite="lax",
        path="/",
        max_age=POS_MAX_AGE,
    )


def resolved_pos(request: Request, index: LibraryIndex) -> str:
    """The remembered directory, or "" for the root.

    Validated against the index on every read, which `resolved_view` does not have to do:
    a cookie holding "grid" cannot go stale and a cookie holding a path can. The directory
    may have been renamed or deleted since, or hand-edited to `.data` — and `index.require`
    answers all three with a 400, which would break the *rail link itself* rather than
    merely inconvenience someone. The index is the whitelist here exactly as it is for
    `_resolve`, so a path it does not vouch for is simply forgotten.

    Cheap: one primary-key SELECT, and an absent or empty cookie never reaches it
    (`normalise` gives "" and `entry("")` answers from memory).
    """
    raw = request.cookies.get(POS_COOKIE)
    if not raw:
        return ""
    try:
        path = normalise(unquote(raw))
    except PathError:
        # `normalise` raises on traversal, which every other caller *wants* -- a `?p=`
        # naming `../etc` is someone trying it on and deserves its 400. Here the value is
        # our own cookie and the request is for some other page entirely, so a 400 would
        # take out /devices and /jobs as well, for a rail link nobody clicked.
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
