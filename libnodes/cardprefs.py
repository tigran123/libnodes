"""Which parts of a GRID device card this browser draws, ticked at /settings.

A cookie, not a `Settings` field: it is a fact about a screen, not the host. It names what
is **hidden**, so no cookie is the full card and a field added later shows by default. The
card template omits a hidden block rather than hiding it with CSS, which `.card`'s flex
display would outrank. A leaf module, because `deps.py` reads it on every render.
"""

from __future__ import annotations

from fastapi import Request

#: Every tickable part of the card, in Settings order: cookie key, label, and what turning
#: it off costs.
#:
#: Not in here, and not tickable: the root `<div id="card-…">`, which the Test dialog's
#: out-of-band swap targets, and `.card-head` -- the dot, the name and the badges. A card
#: with none of those left is not a card.
CARD_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("addr", "Address", "the host and port, e.g. lg:2222"),
    ("target", "Target path", "where on the device books land, e.g. ~/sd/Books"),
    ("space", "Storage", "used / total, and the bar under it"),
    ("sync", "Last sync", "when this device was last pushed to"),
    ("seen", "Last seen", "how old the readings above are"),
    ("battery", "Battery", "charge, charging bolt and the bar — shown only for a device "
                           "that declares where to read it"),
    ("actions", "Test / Actions", "the button row, on every card. Rescan still re-probes "
                                  "the fleet, and a running job keeps its Abort in the "
                                  "dock"),
)

CARD_COOKIE = "libnodes_card_hide"

#: A dot, not a comma: a comma is not a cookie-octet, so http.cookies quoted the value
#: into one unsplittable string that read as "nothing hidden".
SEP = "."

CARD_MAX_AGE = 31536000

#: The full card, a Jinja global: a handler that forgets the context shows everything.
ALL_VISIBLE: dict[str, bool] = {key: True for key, _label, _note in CARD_FIELDS}

_KEYS = frozenset(ALL_VISIBLE)


def _hidden(raw: str | None) -> frozenset[str]:
    """The cookie's keys that exist; unknown ones are dropped."""
    if not raw:
        return frozenset()
    return frozenset(part for part in raw.split(SEP) if part in _KEYS)


def resolved_cards(request: Request) -> dict[str, bool]:
    """Which parts of the card to draw, keyed by `CARD_FIELDS` key."""
    hidden = _hidden(request.cookies.get(CARD_COOKIE))
    return {key: key not in hidden for key in _KEYS}


def hidden_from_form(shown: list[str]) -> str:
    """The cookie value for a submitted form: an unchecked box submits nothing, so the
    hidden set is everything not shown."""
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
