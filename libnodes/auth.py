"""The lock on the front door: one shared password, one signed cookie.

LibNodes drives real hardware. Every button on the Devices page starts an rsync to an
e-reader, aborts a running transfer, or deletes history, and the service binds
0.0.0.0 so that every host on the LAN can reach it. The threat this module answers is
not an attacker -- it is anyone in the house who opens the page and starts pressing
things.

What it guarantees:

  * Nothing outside OPEN_PATHS is served without a valid session cookie. The check is a
    middleware rather than a per-route dependency precisely so that "did you remember to
    protect this one?" is not a question anyone has to answer again -- a route added
    tomorrow is covered by construction.
  * A fragment request never receives a page. See `_deny`.
  * Sessions do not survive a password change, because the signing key *is* the password
    (via `_signing_key`). There is no key file, nothing in var/, and nothing to rotate.

Cookies, not headers, and not negotiable: /jobs/stream and /devices.yaml/stream are read
by EventSource, which cannot send custom headers. The bundled extension already opens
them with `withCredentials: true` (static/htmx-ext-sse.js:76), so a cookie rides along;
an Authorization scheme would have silently killed the sync dock.
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

#: Named like the theme cookie (`libnodes_theme`, set in static/app.js:25) so the pair
#: reads as one family in devtools.
COOKIE = "libnodes_session"

LOGIN_PATH = "/login"

#: The holes in the boundary. Each one is deliberate and each one costs something, so
#: they are listed here rather than scattered across the handlers:
#:
#:   /static     The login page needs app.css and the self-hosted fonts. Serve it behind
#:               the lock and the first thing an unauthenticated visitor sees is an
#:               unstyled form -- and StaticFiles is a mount, so a router dependency
#:               would not have covered it anyway.
#:   /healthz    deploy/deploy.sh:60 polls this for 30 s after every restart, with
#:               `curl -fsS`, and fails the deploy if it does not answer. Lock it and
#:               every deploy hangs and then reports a broken build. It exposes counts,
#:               no control and no names.
#:   /login      Otherwise the redirect target redirects.
#:   /logout     Harmless without a session, and cheaper than special-casing it.
#:
#: Exact paths, with /static the one prefix. A prefix match on the others would open
#: anything merely *starting* with them -- a future /healthz-detail or /logins page would
#: be born unprotected, which is the "covered by construction" claim quietly broken.
OPEN_PATHS = frozenset({"/healthz", "/login", "/logout"})
OPEN_PREFIX = "/static/"

#: Every route that renders a whole page -- i.e. the ones whose template extends
#: base.html. Everything else in the app is an HTMX fragment, and the only thing this set
#: is for is keeping `safe_next` from delivering a browser to one. Keep it in step with
#: the page handlers; a page missing from here still works, it just sends you to
#: /devices after login instead of back where you were.
PAGES = frozenset(
    {
        "/",
        "/devices",
        "/library",
        "/jobs",
        "/devices.yaml",
        "/presets",
        "/keys",
        "/device/new",
    }
)


def _signing_key(password: str) -> bytes:
    """Derive the cookie key from the password itself.

    Not a stored secret: making the key a function of the password means changing
    LIBNODES_PASSWORD and restarting invalidates every outstanding session, with no key
    file to write into var/, no state to lose on a fresh checkout, and no way for a
    revoked password to keep working because a browser still holds a cookie.

    blake2b takes a key natively, so no HMAC construction is needed -- and it is already
    the primitive that names every blob in the library vault.
    """
    return hashlib.blake2b(
        password.encode("utf-8"), person=b"libnodes-key", digest_size=32
    ).digest()


def _sign(expiry: int, key: bytes) -> str:
    return hashlib.blake2b(
        str(expiry).encode("ascii"), key=key, digest_size=16
    ).hexdigest()


def mint(password: str, ttl: float) -> str:
    """`1789458123.9f86d081…` -- an expiry and its signature, nothing else.

    The cookie carries no identity because there is none to carry: one password, one
    household. Anything else in here would be state the server has to trust the client
    about.
    """
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
    # compare_digest, not ==, so a forged cookie cannot be refined one byte at a time
    # against the response latency.
    return hmac.compare_digest(signature, _sign(expiry, _signing_key(password)))


def check_password(given: str, configured: str) -> bool:
    # Compared as utf-8 bytes, not as str: compare_digest raises TypeError on a str
    # holding any non-ASCII character, so a password with an accent or a Cyrillic letter
    # in it would 500 the login handler and lock the owner out of their own fleet.
    return bool(configured) and secrets.compare_digest(
        given.encode("utf-8"), configured.encode("utf-8")
    )


def is_open(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIX)


def authenticated(request: Request, password: str) -> bool:
    token = request.cookies.get(COOKIE)
    return bool(token) and verify(token, password)


def safe_next(target: str | None) -> str:
    """Where to land after login. Anything that is not a real page becomes /devices.

    Two separate refusals, and the second is the one that came from a real bug:

    * Not off-site. `//evil.example` and `https://evil.example` are both absolute to a
      browser, so the test is "one leading slash and no scheme", not "starts with a
      slash".
    * Not a fragment. Almost every route in this app returns a bare table body with no
      <html> around it, and delivering a browser to one is a broken page, not a
      destination. A tab left open across a restart polls `/devices/rows`, that poll is
      answered with a redirect, and login then honoured `next=/devices/rows` and dropped
      the user on an unstyled wall of device names -- reported from a real session.

    An allowlist rather than a pattern, because "is this a whole page?" is not something
    a path can be asked. Getting it wrong costs a trip to /devices, which is where the
    app opens anyway.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/devices"
    # A backslash is a slash to some URL parsers but not to others; refuse rather than
    # guess which one the browser is.
    if "\\" in target or "\n" in target or "\r" in target:
        return "/devices"
    if urlsplit(target).path not in PAGES:
        return "/devices"
    return target


def _current_page(request: Request) -> str | None:
    """The page an htmx request was fired from, per its HX-Current-URL header.

    The request's own path is a fragment endpoint -- `/devices/rows`, `/jobs/dock` -- and
    is never where the user thinks they are. htmx sends `location.href` on every request
    (verified in the vendored 2.0.4), so this is the only honest answer to "where were
    they?". Only the path and query are kept; the header names an absolute URL and its
    host is the client's claim, not ours to trust.
    """
    raw = request.headers.get("HX-Current-URL")
    if not raw:
        return None
    parts = urlsplit(raw)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _deny(request: Request) -> Response:
    """Turn away one unauthenticated request, in the one way its caller understands.

    An HTMX request gets 401 + HX-Redirect and an *empty body*. Both halves matter:

      * htmx acts on HX-Redirect before it consults the status code -- in the bundled
        2.0.4 the response handler navigates and returns above the swap decision -- so
        the honest 401 costs nothing and no client-side error handling is needed. The
        app has none today; without the header a 401 makes every button do nothing at
        all, silently.
      * The body is empty because a fragment response must never be a document. A login
        page returned to an hx-get would be swapped into a table row, which is exactly
        what test_fragments_render_standalone exists to forbid.

    The redirect also fixes the stale-tab case that the /fleet 308s in main.py:54 were
    added for: a page left open overnight polls every 10 s, and the poll carries it to
    the login screen rather than leaving a frozen table and no explanation.
    """
    if request.headers.get("HX-Request") == "true":
        # Not request.url.path: that is the fragment being polled, not the page the user
        # is looking at. See _current_page.
        target = safe_next(_current_page(request))
        login = f"{LOGIN_PATH}?next={quote(target, safe='')}"
        return Response(status_code=401, headers={"HX-Redirect": login})

    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    login = f"{LOGIN_PATH}?next={quote(target, safe='')}"
    # 303, not 307: whatever the method was, the browser should GET the login page.
    return RedirectResponse(login, status_code=303)


class AuthMiddleware:
    """Pure ASGI, not BaseHTTPMiddleware, and that is load-bearing.

    BaseHTTPMiddleware wraps the response in a queue to hand it back as a single object,
    which breaks EventSourceResponse: the dock would stop streaming and start arriving in
    lumps, the same symptom nginx's proxy_buffering produces on an SSE endpoint.
    jobs.py:424 carries the matching warning about the receive channel.

    This class reads the scope and nothing else. It either calls the app unchanged --
    passing `send` straight through, so a stream stays a stream byte for byte -- or it
    answers by itself. It never wraps `send` and never touches `receive`, and the Request
    it builds is constructed without one so that it *cannot* consume a body.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Per request, not captured in __init__: get_settings is lru_cached and tests
        # repoint it with reset_caches(), so a value read once at construction would
        # freeze the first test's password into every later one.
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
