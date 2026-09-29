"""Settings: per-browser display preferences (see `cardprefs.py`). A plain form and a
303, so it works without script and adds no fragment route."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..cardprefs import CARD_COOKIE, CARD_FIELDS, CARD_MAX_AGE, hidden_from_form
from ..deps import base_context
from ..templating import templates

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """The ticks as this browser last saved them. Opening it writes nothing."""
    ctx = base_context(request, "settings")
    ctx["card_fields"] = CARD_FIELDS
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings")
async def save_settings(request: Request, show: list[str] = Form(default=[])):
    """Save the ticks and come back to them."""
    response = RedirectResponse("/settings", status_code=303)
    response.set_cookie(
        CARD_COOKIE,
        hidden_from_form(show),
        # No `secure` on plain http; httponly, as only the server reads it.
        httponly=True,
        samesite="lax",
        path="/",
        max_age=CARD_MAX_AGE,
    )
    return response
