"""The login page and the actions that open and close a session.

The one page that does not extend base.html, whose context would show a visitor the
fleet's hostnames; and it loads no script, so the lock works with scripting off.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..auth import COOKIE, authenticated, check_password, mint, safe_next
from ..deps import state
from ..templating import templates

router = APIRouter()


def _login_context(request: Request, error: str = "") -> dict:
    """Deliberately thin: nothing base_context collects may be shown here."""
    return {
        "request": request,
        "theme": "light" if request.cookies.get("libnodes_theme") == "light" else "dark",
        "error": error,
        "next": safe_next(request.query_params.get("next")),
    }


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    app = state(request)
    password = app.settings.password.get_secret_value()
    # No lock configured, or already through it: there is nothing to ask for.
    if not password or authenticated(request, password):
        return RedirectResponse(safe_next(request.query_params.get("next")), 303)
    return templates.TemplateResponse(request, "login.html", _login_context(request))


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    password: str = Form(""),
    remember: str = Form(""),
    next: str = Form(""),
):
    app = state(request)
    configured = app.settings.password.get_secret_value()
    if not configured:
        return RedirectResponse("/devices", 303)

    if not check_password(password, configured):
        # A flat delay: half a second for a typo, and no fast oracle.
        await asyncio.sleep(0.5)
        return templates.TemplateResponse(
            request,
            "login.html",
            _login_context(request, error="That is not the password."),
            status_code=401,
        )

    ttl = app.settings.session_days * 86400
    response = RedirectResponse(safe_next(next), 303)
    response.set_cookie(
        COOKIE,
        mint(configured, ttl),
        # No `secure`: on plain http the login would appear to succeed and change nothing.
        httponly=True,
        samesite="lax",
        path="/",
        max_age=int(ttl) if remember else None,
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    response = RedirectResponse("/login", 303)
    # The same path it was set with, or the browser keeps the cookie.
    response.delete_cookie(COOKIE, path="/")
    return response
