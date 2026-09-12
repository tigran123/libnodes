"""Which parts of a GRID device card this browser wants to see.

A leaf module on purpose: `deps.py` reads it on every single render, and `deps.py` is what
every route module imports. Putting these in `routes/settings.py` would make the shared
module import a *route* module on that hot path, a cycle held open by a lazy import for no
gain — the one already in `deps.py` carries a comment saying it is forced, and this one
would not be.

Deliberately **not** a `Settings` field. `LIBNODES_*` settings are facts about the host;
this is a fact about the screen you are looking at. The tablet wants a battery wall and
the desktop wants the full card, and they are the same service — so it is a cookie, like
the theme and the TABLE/GRID choice, and not an environment variable that would give the
whole LAN one shared card layout.

The cookie names what is **hidden**, not what is shown, and the name says so. Two reasons:
no cookie then means "the card as it has always been", so a browser that never opens
Settings is untouched; and a field added here later is visible by default rather than
silently missing from every card until someone re-ticks it.

Nothing here hides anything with CSS. `.card` is `display: flex`, which outranks both a
`.is-hidden` class and the UA's own `[hidden]` — the mistake CLAUDE.md records under "when
a change is visual, assert on computed style". The card template simply does not emit the
block, which is a thing a rendered-HTML test can see.
"""

from __future__ import annotations

from fastapi import Request

#: Every tickable part of the card, in the order the Settings page lists them: the key
#: that travels in the cookie, the label beside the tick, and what turning it off costs.
#: One list, so the page and `device_card.html` cannot drift apart —
#: `tests/test_card_prefs.py` renders one tick per entry and asserts exactly that.
#:
#: Not in here, and not tickable: the root `<div id="card-…">`, which is the `hx-target`
#: of the card's own Retry and of the Test dialog's out-of-band swap, and `.card-head` —
#: the dot, the name and the badges. A card with none of those left is not a card.
CARD_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("addr", "Address", "the host and port, e.g. lg:2222"),
    ("target", "Target path", "where on the device books land, e.g. ~/sd/Books"),
    ("space", "Storage", "used / total, and the bar under it"),
    ("sync", "Last sync", "when this device was last pushed to"),
    ("seen", "Last seen", "how old the readings above are"),
    ("battery", "Battery", "charge, charging bolt and the bar — shown only for a device "
                           "that declares where to read it"),
    ("actions", "Test / Actions", "the button row. Kept anyway on a card that is red or "
                                  "syncing: Retry is the only per-device re-probe in "
                                  "GRID, and Abort the only way to stop a push"),
)

#: Named for its polarity. `libnodes_card=addr.target` reads 50/50 as "show these", and
#: which way round it is happens to be the load-bearing part of the design.
CARD_COOKIE = "libnodes_card_hide"

#: A dot, not a comma. A comma is not a cookie-octet (RFC 6265), so Python's http.cookies
#: quotes the whole value and escapes it -- `set_cookie` emitted
#: `"addr\054target\054seen"`, which comes back as one unsplittable string and reads as
#: "nothing hidden". Caught by test_saving_nothing_hides_everything, which is the only
#: reason it was ever seen. `.` is in http.cookies' own legal set and needs no quoting.
SEP = "."

#: A year, matching `libnodes_view` (routes/devices.py) and the theme cookie (app.js):
#: all three are display preferences and none is worth asking twice.
CARD_MAX_AGE = 31536000

#: The card as it was before any of this existed. Registered as a Jinja global in
#: `templating.py`, so a handler that forgets to build a context degrades to *everything
#: visible* rather than rendering a bare title that no test would notice.
ALL_VISIBLE: dict[str, bool] = {key: True for key, _label, _note in CARD_FIELDS}

_KEYS = frozenset(ALL_VISIBLE)


def _hidden(raw: str | None) -> frozenset[str]:
    """The cookie's keys, intersected with the ones that exist.

    `resolved_view` may trust its cookie outright because nothing but an explicit `?view=`
    ever writes it. This one is written from a form post and can be hand-edited to
    anything, so unknown names are dropped rather than carried around. An absent or empty
    value hides nothing, which is the same answer by two different routes.
    """
    if not raw:
        return frozenset()
    return frozenset(part for part in raw.split(SEP) if part in _KEYS)


def resolved_cards(request: Request) -> dict[str, bool]:
    """Which parts of the card to draw, keyed by `CARD_FIELDS` key."""
    hidden = _hidden(request.cookies.get(CARD_COOKIE))
    return {key: key not in hidden for key in _KEYS}


def hidden_from_form(shown: list[str]) -> str:
    """The cookie value for a submitted form.

    An unchecked box submits nothing at all, so what arrives is the *shown* set and the
    hidden one is everything else. That is exactly the semantics wanted, and it is also
    why the cookie cannot be written from a form that failed to render: every key missing
    would read as every field hidden, which is why only `POST /settings` writes it.
    """
    keep = {key for key in shown if key in _KEYS}
    return SEP.join(key for key, _label, _note in CARD_FIELDS if key not in keep)


__all__ = [
    "ALL_VISIBLE",
    "CARD_COOKIE",
    "CARD_FIELDS",
    "CARD_MAX_AGE",
    "SEP",
    "hidden_from_form",
    "resolved_cards",
]
