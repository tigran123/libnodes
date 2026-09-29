"""The lock on the front door: one shared password, one signed cookie.

The service binds 0.0.0.0 and every button drives real hardware. Guarantees: nothing
outside OPEN_PATHS is served without a valid cookie (a middleware, so a route added later
is covered by construction); a fragment request never receives a page (`_deny`); and a
password change ends every session, the signing key being derived from it. A cookie, not a
header, because /jobs/stream is an EventSource, which cannot send one.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from urllib.parse import quote, urlsplit

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import get_settings

COOKIE = "libnodes_session"

LOGIN_PATH = "/login"

#: The deliberate holes, all in one place: /static (the login page needs its styles),
#: /healthz (restart checks poll it; counts only, no names), /login and /logout. Exact
#: paths, /static the one prefix, so a future /healthz-detail is not born unprotected.
OPEN_PATHS = frozenset({"/healthz", "/login", "/logout"})
OPEN_PREFIX = "/static/"

#: The whole pages, where `safe_next` may send a browser after login. A page missing here
#: still works; login just lands on /devices instead.
PAGES = frozenset(
    {
        "/",
        "/devices",
        "/library",
        "/jobs",
        "/settings",
    }
)


def _signing_key(password: str) -> bytes:
    """The cookie key, derived from the password: changing it ends every session, with
    no key file to keep. blake2b is keyed natively, so no HMAC is needed."""
    return hashlib.blake2b(
        password.encode("utf-8"), person=b"libnodes-key", digest_size=32
    ).digest()


def _sign(expiry: int, key: bytes) -> str:
    return hashlib.blake2b(
        str(expiry).encode("ascii"), key=key, digest_size=16
    ).hexdigest()


def mint(password: str, ttl: float) -> str:
    """`1789458123.9f86d081…`: an expiry and its signature. One password, so no identity."""
    expiry = int(time.time() + ttl)
    return f"{expiry}.{_sign(expiry, _signing_key(password))}"


def verify(token: str, password: str) -> bool:
    """True only for a signature we produced, over an expiry still in the future."""
    raw, _, signature = token.partition(".")
    if not signature:
        return False
    try:
        expiry = int(raw)
    except ValueError:
        return False
    if expiry <= time.time():
        return False
    return hmac.compare_digest(signature, _sign(expiry, _signing_key(password)))


def check_password(given: str, configured: str) -> bool:
    # As bytes: compare_digest raises on a non-ASCII str, which locked out a Cyrillic password.
    return bool(configured) and secrets.compare_digest(
        given.encode("utf-8"), configured.encode("utf-8")
    )


def is_open(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIX)


def authenticated(request: Request, password: str) -> bool:
    token = request.cookies.get(COOKIE)
    return bool(token) and verify(token, password)


def safe_next(target: str | None) -> str:
    """Where to land after login: a real page, else /devices. Never off-site (`//x` is
    absolute to a browser) and never a fragment -- a stale tab's poll once sent login to
    `/devices/rows`, an unstyled wall of names."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/devices"
    # A backslash is a slash to some parsers.
    if "\\" in target or "\n" in target or "\r" in target:
        return "/devices"
    if urlsplit(target).path not in PAGES:
        return "/devices"
    return target


def _current_page(request: Request) -> str | None:
    """The page an htmx request came from (HX-Current-URL), path and query only -- the
    request's own path is a fragment endpoint."""
    raw = request.headers.get("HX-Current-URL")
    if not raw:
        return None
    parts = urlsplit(raw)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _deny(request: Request) -> Response:
    """Turn away one unauthenticated request in the way its caller understands.

    htmx gets 401 + HX-Redirect and an empty body: htmx follows the header before looking
    at the status (verified in 2.0.4), and a login page swapped into a table row is what
    the standalone-fragment contract forbids. A browser gets a 303 to /login.
    """
    if request.headers.get("HX-Request") == "true":
        target = safe_next(_current_page(request))
        login = f"{LOGIN_PATH}?next={quote(target, safe='')}"
        return Response(status_code=401, headers={"HX-Redirect": login})

    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    login = f"{LOGIN_PATH}?next={quote(target, safe='')}"
    return RedirectResponse(login, status_code=303)


class AuthMiddleware:
    """Pure ASGI, not BaseHTTPMiddleware, which buffers the response and would make the SSE
    dock arrive in lumps. Reads the scope only; never wraps `send` or touches `receive`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Per request: tests repoint the cached settings.
        settings = get_settings()
        password = settings.password.get_secret_value()

        if not password or is_open(scope["path"]):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        if authenticated(request, password):
            await self.app(scope, receive, send)
            return

        await _deny(request)(scope, receive, send)


__all__ = [
    "COOKIE",
    "LOGIN_PATH",
    "OPEN_PATHS",
    "OPEN_PREFIX",
    "PAGES",
    "AuthMiddleware",
    "authenticated",
    "check_password",
    "is_open",
    "mint",
    "safe_next",
    "verify",
]
