"""Settings: per-browser display preferences.

Only HTTP lives here. What the preferences *are* is `libnodes/cardprefs.py`, which is a
leaf module because `deps.py` reads it on every render — see the docstring there for why
this is a cookie rather than a `LIBNODES_*` setting.

A plain form and a redirect, not htmx. The page has to work with scripting off, the way
`/logout` and the login form already do, and there is nothing here a swap does better than
a 303. It also keeps this out of `FRAGMENTS` in `tests/test_routes.py`: no new fragment
route means no new standalone-render contract to remember to enforce.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..cardprefs import CARD_COOKIE, CARD_FIELDS, CARD_MAX_AGE, hidden_from_form
from ..deps import base_context
from ..templating import templates

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """The ticks, in whatever state this browser last saved.

    Writes no cookie. A page that pins its own default merely by being opened is the bug
    `/devices` documents from the far side: arriving by the rail link must not decide
    anything.
    """
    ctx = base_context(request, "settings")
    ctx["card_fields"] = CARD_FIELDS
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings")
async def save_settings(request: Request, show: list[str] = Form(default=[])):
    """Save the ticks and come back to them.

    An unchecked box submits nothing, so `show` is what survived and everything else is
    hidden — `hidden_from_form` does that subtraction against `CARD_FIELDS`, which is also
    what stops a hand-made POST from writing keys that mean nothing.
    """
    response = RedirectResponse("/settings", status_code=303)
    response.set_cookie(
        CARD_COOKIE,
        hidden_from_form(show),
        # The `libnodes_view` flags, for the same reasons: no `secure`, because LibNodes
        # is plain http on the LAN and a Secure cookie would simply never be stored; and
        # httponly because nothing on the client reads this one -- the server both writes
        # it and renders from it.
        httponly=True,
        samesite="lax",
        path="/",
        max_age=CARD_MAX_AGE,
    )
    return response
