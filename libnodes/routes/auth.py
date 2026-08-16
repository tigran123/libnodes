"""The login page and the two actions that open and close a session.

The page is the one full page in the app that does *not* extend base.html, and that is
the point rather than an oversight: base.html renders the rail, the dock, host telemetry
and the whole device list, all of it assembled by deps.base_context. None of that may be
built for someone who has not logged in yet -- an unauthenticated visitor should not be
able to read the fleet's hostnames off the login screen.

It also loads no JavaScript. A plain form posts the password, so the lock works with
scripting off, and there is nothing here for htmx to swap.
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
    """Deliberately thin. Compare deps.base_context -- everything it collects is
    something this page must not show."""
    return {
        "request": request,
        # Same server-side stamp as base.html:4, so the login page is already in the
        # right palette on first paint instead of flashing the other one.
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
        # A flat delay, not rate limiting: it costs an honest typo half a second and
        # makes the endpoint useless as a fast oracle. Anything cleverer would be state
        # to keep for a threat model that does not have an adversary in it.
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
        # No `secure`: LibNodes is served over plain http on the LAN -- there is no TLS
        # and no reverse proxy in front (see deploy/README.md). A Secure cookie would
        # never be stored and the login would appear to succeed and change nothing.
        # app.js:37 records the same constraint about navigator.clipboard.
        httponly=True,
        samesite="lax",
        path="/",
        # Unticked means a session cookie, so a browser that is closed forgets. Ticked
        # means the cookie outlives it, which is the whole point of asking once.
        max_age=int(ttl) if remember else None,
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    response = RedirectResponse("/login", 303)
    # delete_cookie must be given the same path the cookie was set with, or the browser
    # keeps the original and logging out silently does nothing.
    response.delete_cookie(COOKIE, path="/")
    return response
