"""The lock on the front door.

The threat model is a child on the home LAN, not an attacker, so these tests are about
the two ways a lock like this fails in practice: it is not actually applied to something
(the fragment routes, the SSE streams, the POST actions), or it is applied to something
it must not be (/static, /healthz) and takes the app or the deploy down with it.

The suite's other ~124 client-using tests run with no password set and are untouched by
any of this. `test_no_password_means_no_lock` pins that on purpose -- it is the fail-open
default, and it should break loudly if it is ever inverted by accident.
"""

from __future__ import annotations

import html
import re
import time
from pathlib import Path

import httpx
import pytest

from libnodes.auth import COOKIE, mint, verify

PASSWORD = "correct horse"


@pytest.fixture
async def locked(settings, devices_file, index, monkeypatch):
    """A second app, with the lock on.

    Built here rather than in conftest so that turning the password on cannot leak into
    any other test: settings are lru_cached, so the env var plus reset_caches() has to
    happen before create_app().
    """
    from libnodes.config import get_devices, get_settings, reset_caches
    from libnodes.main import create_app

    monkeypatch.setenv("LIBNODES_PASSWORD", PASSWORD)
    reset_caches()
    get_settings()
    get_devices.cache_clear()

    app = create_app()
    app.state.lib.index.reindex()
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        # follow_redirects stays off (as in conftest), so a bounce to /login shows up as
        # an assertable 303 rather than quietly succeeding.
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.app = app
            yield c


async def _login(client, password=PASSWORD, **extra):
    return await client.post(
        "/login", data={"password": password, "remember": "1", **extra}
    )


# --- the default -------------------------------------------------------------


async def test_no_password_means_no_lock(client):
    """Fail-open is deliberate: it is what leaves the dev server and the other 317 tests
    alone. If this ever starts failing, the default changed and every other test in the
    suite is about to."""
    r = await client.get("/devices")
    assert r.status_code == 200


# --- what is refused ---------------------------------------------------------


async def test_a_page_bounces_to_login(locked):
    r = await locked.get("/devices")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fdevices"


async def test_a_fragment_gets_a_header_not_a_page(locked):
    """The HTMX contract survives the lock.

    A login *page* returned to an hx-get would be swapped into a table row -- the exact
    thing test_fragments_render_standalone forbids. htmx acts on HX-Redirect before it
    consults the status code, so the honest 401 still navigates.
    """
    r = await locked.get("/devices/rows", headers={"HX-Request": "true"})
    assert r.status_code == 401
    assert r.headers["HX-Redirect"].startswith("/login?next=")
    assert "<!doctype" not in r.text.lower()
    assert "<html" not in r.text.lower()
    assert r.text == ""


async def test_a_push_cannot_be_started(locked):
    """The one that is actually the point: nobody else gets to start an rsync."""
    before = len(locked.app.state.lib.store.recent(50))
    r = await locked.post(
        "/jobs",
        data={"device": "kobo", "path": "Fiction"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 401
    assert len(locked.app.state.lib.store.recent(50)) == before


async def test_the_job_stream_is_closed(locked):
    """SSE is the one place a cookie scheme has to work and a header scheme could not.
    No other test in the suite opens either stream."""
    r = await locked.get("/jobs/stream")
    assert r.status_code != 200


async def test_the_config_stream_is_closed(locked):
    r = await locked.get("/devices.yaml/stream")
    assert r.status_code != 200


async def test_the_raw_config_download_is_closed(locked):
    """It is the whole devices.yaml: hostnames, users, ports, target paths."""
    r = await locked.get("/devices.yaml/raw")
    assert r.status_code == 303


async def test_the_old_urls_are_closed_too(locked):
    """The /fleet and /node redirects (main.py:54) are routes like any other. A router
    dependency would have missed them; the middleware does not."""
    for path in ("/fleet/rows", "/node/kobo/row"):
        assert (await locked.get(path)).status_code == 303


# --- what stays open ---------------------------------------------------------


async def test_healthz_stays_open(locked):
    """deploy/deploy.sh:60 polls this for 30 s after every restart and fails the deploy
    if it does not answer. Locking it turns every deploy into a false alarm."""
    r = await locked.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


async def test_static_stays_open(locked):
    """Otherwise the first thing an unauthenticated visitor sees is an unstyled form."""
    r = await locked.get("/static/app.css")
    assert r.status_code == 200


def test_open_paths_are_exact_not_prefixes():
    """A prefix match would open anything merely starting with an open path, so a future
    /healthz-detail would be born unprotected."""
    from libnodes.auth import is_open

    assert is_open("/healthz") and is_open("/login") and is_open("/static/app.css")
    assert not is_open("/healthz-detail")
    assert not is_open("/logins")
    assert not is_open("/devices")


async def test_the_login_page_names_no_devices(locked):
    """Nothing about the fleet before the password. The device names and hosts here come
    from the devices_file fixture."""
    body = (await locked.get("/login")).text
    for leak in ("Test Kobo", "Test Phone", "/mnt/onboard", "u0_a1", "8022"):
        assert leak not in body


# --- getting in and out ------------------------------------------------------


async def test_the_password_opens_it(locked):
    r = await _login(locked)
    assert r.status_code == 303
    assert COOKIE in r.cookies
    assert (await locked.get("/devices")).status_code == 200


async def test_a_wrong_password_does_not(locked):
    r = await _login(locked, password="incorrect horse")
    assert r.status_code == 401
    assert COOKIE not in r.cookies
    assert (await locked.get("/devices")).status_code == 303


async def test_an_empty_password_does_not(locked):
    """check_password() must not treat "" as a match against a configured secret."""
    assert (await _login(locked, password="")).status_code == 401


def test_a_non_ascii_password_works():
    """compare_digest raises TypeError on a str holding any non-ASCII character, so
    comparing as str would 500 the login handler and lock the owner out of their own
    fleet. The library this drives is multilingual; the password may be too."""
    from libnodes.auth import check_password

    assert check_password("Ürantia-пароль", "Ürantia-пароль")
    assert not check_password("Ürantia-пароль", "something else")
    # And the signing key must survive the same round trip.
    assert verify(mint("Ürantia-пароль", 60), "Ürantia-пароль")


async def test_remember_is_what_outlives_the_browser(locked):
    """Ticked writes Max-Age so the cookie survives a browser restart; unticked leaves it
    a session cookie. Both must still open the app right now."""
    remembered = await locked.post("/login", data={"password": PASSWORD, "remember": "1"})
    assert "max-age=" in remembered.headers["set-cookie"].lower()
    assert (await locked.get("/devices")).status_code == 200

    locked.cookies.clear()
    just_now = await locked.post("/login", data={"password": PASSWORD})
    assert "max-age=" not in just_now.headers["set-cookie"].lower()
    assert (await locked.get("/devices")).status_code == 200


async def test_logout_closes_it_again(locked):
    await _login(locked)
    assert (await locked.get("/devices")).status_code == 200
    r = await locked.post("/logout")
    assert r.status_code == 303
    assert (await locked.get("/devices")).status_code == 303


async def test_login_returns_you_to_where_you_were(locked):
    """The whole round trip, because the encoding changes hands twice: the middleware
    percent-encodes the path into the redirect, the page renders it decoded into a hidden
    field, and the browser posts that. Asserting on the header alone would miss a
    mismatch between the two ends."""
    bounce = await locked.get("/library?q=feynman")
    page = await locked.get(bounce.headers["location"])
    carried = re.search(r'name="next" value="([^"]*)"', page.text).group(1)
    assert carried == "/library?q=feynman"

    r = await _login(locked, next=html.unescape(carried))
    assert r.headers["location"] == "/library?q=feynman"


async def test_login_never_lands_on_a_fragment(locked):
    """The reported bug, end to end.

    A tab left open across the restart polls /devices/rows every 10s. That poll was
    answered with a redirect carrying next=/devices/rows, and login honoured it -- so
    signing in delivered an unstyled wall of device names with no page around it.
    """
    poll = await locked.get(
        "/devices/rows",
        headers={"HX-Request": "true", "HX-Current-URL": "http://pi:8090/devices"},
    )
    login_url = poll.headers["HX-Redirect"]
    assert "next=%2Fdevices%2Frows" not in login_url, "the fragment became the target"

    page = await locked.get(login_url)
    carried = re.search(r'name="next" value="([^"]*)"', page.text).group(1)
    landed = await _login(locked, next=html.unescape(carried))
    assert landed.headers["location"] == "/devices"

    # And the page it lands on is a whole document, which is the actual complaint.
    assert "<!doctype" in (await locked.get("/devices")).text.lower()


async def test_a_poll_sends_you_back_to_the_page_you_were_reading(locked):
    """Not merely /devices: htmx names the real one in HX-Current-URL, so a session that
    expires while you are filtering the library returns you to the filter."""
    poll = await locked.get(
        "/lib/list",
        headers={
            "HX-Request": "true",
            "HX-Current-URL": "http://pi:8090/library?q=feynman",
        },
    )
    assert poll.headers["HX-Redirect"] == "/login?next=%2Flibrary%3Fq%3Dfeynman"


async def test_a_poll_with_no_current_url_falls_back(locked):
    """HX-Current-URL is sent by every htmx request, but nothing may depend on a header
    the client controls being present."""
    poll = await locked.get("/devices/rows", headers={"HX-Request": "true"})
    assert poll.headers["HX-Redirect"] == "/login?next=%2Fdevices"


def test_only_whole_pages_are_valid_destinations():
    """Directly, because the allowlist is the guard and a stale bookmark or an old link
    can still carry a fragment path into ?next=."""
    from libnodes.auth import safe_next

    for fragment in ("/devices/rows", "/jobs/dock", "/lib/list", "/device/kobo/menu",
                     "/jobs/stream", "/devices.yaml/raw"):
        assert safe_next(fragment) == "/devices", fragment
    for page in ("/library", "/jobs", "/devices.yaml", "/library?q=x"):
        assert safe_next(page) == page, page


async def test_every_allowlisted_page_really_is_one(locked):
    """Keeps PAGES honest. A path listed here that is secretly a fragment would put the
    original bug straight back, and one that 404s would send you nowhere."""
    from libnodes.auth import PAGES

    await _login(locked)
    for path in sorted(PAGES):
        r = await locked.get(path)
        if r.status_code in (303, 307):
            continue                      # "/" redirects to /devices by design
        assert r.status_code == 200, path
        assert "<!doctype" in r.text.lower(), path


async def test_next_cannot_leave_the_site(locked):
    """?next=https://elsewhere would make the login form an open redirect."""
    for hostile in ("https://evil.example/x", "//evil.example/x", "/\\evil.example"):
        r = await _login(locked, next=hostile)
        assert r.headers["location"] == "/devices"


async def test_the_login_page_steps_aside_when_already_in(locked):
    await _login(locked)
    r = await locked.get("/login")
    assert r.status_code == 303


# --- the reveal eye ----------------------------------------------------------


async def test_the_password_field_is_masked_by_default(locked):
    """The eye reveals on demand; it must never ship already revealed. A regression to
    type="text" would put the password on screen for anyone walking past."""
    body = (await locked.get("/login")).text
    assert 'type="password"' in body
    assert 'aria-pressed="false"' in body


async def test_the_eye_cannot_submit_the_form(locked):
    """A bare <button> inside a form defaults to submit, so without type="button"
    pressing the eye posts the half-typed password instead of showing it."""
    body = (await locked.get("/login")).text
    eye = body.split('class="pw-reveal"')[1].split(">")[0]
    assert 'type="button"' in eye


async def test_the_eye_points_at_the_field_it_toggles(locked):
    """app.js resolves the input through aria-controls, so a rename on either side makes
    the eye inert -- and inert in a way nothing else would notice."""
    body = (await locked.get("/login")).text
    controls = re.search(r'data-pw-reveal[^>]*aria-controls="([^"]+)"', body).group(1)
    assert f'id="{controls}"' in body

    app_js = (Path(__file__).resolve().parent.parent
              / "libnodes" / "static" / "app.js").read_text()
    assert 'getAttribute("aria-controls")' in app_js


async def test_the_login_page_loads_the_script_that_drives_the_eye(locked):
    """The page is otherwise scriptless. Drop this and the eye renders but never works."""
    assert "app.js?v=" in (await locked.get("/login")).text


def test_the_eye_is_hidden_until_the_script_has_run():
    """Progressive enhancement, asserted on the stylesheet because there is no browser
    here. Without app.js the button cannot toggle anything, and an inert control is worse
    than none -- the login form still works as a plain post with scripting off.

    Deliberately not [hidden]: a display rule outranks the UA's [hidden], which is the
    trap already recorded in CLAUDE.md.
    """
    css = (Path(__file__).resolve().parent.parent
           / "libnodes" / "static" / "app.css").read_text()
    block = css.split("\n.pw-reveal {")[1].split("}")[0]
    assert "display: none" in block
    assert "[data-js] .pw-reveal" in css

    app_js = (Path(__file__).resolve().parent.parent
              / "libnodes" / "static" / "app.js").read_text()
    assert 'setAttribute("data-js"' in app_js


# --- the cookie itself -------------------------------------------------------


async def test_a_forged_signature_is_rejected(locked):
    expiry = int(time.time() + 3600)
    locked.cookies.set(COOKIE, f"{expiry}.{'0' * 32}")
    assert (await locked.get("/devices")).status_code == 303


async def test_a_stretched_expiry_is_rejected(locked):
    """The expiry is signed, not merely carried, so it cannot be edited in the browser."""
    await _login(locked)
    token = locked.cookies[COOKIE]
    _, _, signature = token.partition(".")
    locked.cookies.set(COOKIE, f"{int(time.time() + 999999)}.{signature}")
    assert (await locked.get("/devices")).status_code == 303


async def test_a_shapeless_cookie_is_rejected(locked):
    for junk in ("", "nonsense", ".", "abc.def", f"{int(time.time()+99)}."):
        locked.cookies.set(COOKIE, junk)
        assert (await locked.get("/devices")).status_code == 303


def test_an_expired_token_is_rejected():
    assert verify(mint(PASSWORD, 60), PASSWORD)
    assert not verify(mint(PASSWORD, -1), PASSWORD)


def test_changing_the_password_invalidates_every_session():
    """The signing key is derived from the password, so this is free -- there is no
    session list to revoke and nothing in var/ to clear."""
    token = mint(PASSWORD, 3600)
    assert verify(token, PASSWORD)
    assert not verify(token, PASSWORD + "!")


async def test_an_expired_session_still_gets_told(locked):
    """A tab left open overnight must be carried to the login page, not left polling a
    frozen table -- the failure the /fleet 308s in main.py:54 exist to prevent."""
    locked.cookies.set(COOKIE, mint(PASSWORD, -1))
    r = await locked.get("/devices/rows", headers={"HX-Request": "true"})
    assert r.status_code == 401
    assert "HX-Redirect" in r.headers


# --- the password does not leak ----------------------------------------------


def test_the_password_is_not_printable(locked):
    """base_context puts the whole settings object into every template context
    (deps.py:38), so a stray {{ settings }} would print it into a page."""
    settings = locked.app.state.lib.settings
    assert PASSWORD not in repr(settings)
    assert PASSWORD not in str(settings.password)
    assert settings.password.get_secret_value() == PASSWORD


async def test_the_password_is_not_in_a_page(locked):
    await _login(locked)
    for path in ("/devices", "/library", "/jobs", "/devices.yaml"):
        assert PASSWORD not in (await locked.get(path)).text
