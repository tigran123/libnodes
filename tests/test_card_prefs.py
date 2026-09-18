"""Which parts of a GRID card this browser draws.

The card was a fixed shape: nine lines and a button row, ~300px, whatever you happened to
be watching. These ticks make it answer one question at a time -- with only Battery left
it is about a third of that, so nine devices fit a screen that used to hold three.

Every one of these can break silently. A field that stops rendering leaves a card that
still looks like a card, and a preference read the wrong way round leaves a *blank* one
that no other test in the suite would notice, because every other test runs with no cookie
at all.
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace

import pytest

from libnodes.cardprefs import CARD_COOKIE, CARD_FIELDS, SEP
from libnodes.probe import Battery, Reachability

#: How each field shows up in the rendered card. A marker, not a whole line: the point is
#: to tell "this block rendered" from "this block did not" without pinning the markup any
#: harder than the tests that already pin it.
#:
#: `sync` is the odd one because it shares a `.stat` with `space`: the age is the only
#: unclassed <span> a stat ever holds, the label in `seen` and `battery` carrying
#: `t-muted`. So the regex is what tells the left half of that line from the right.
MARKERS = {
    "addr": "127.0.0.1:2222",
    "target": 'title="/mnt/onboard/Books"',
    "space": "track-disk",
    "sync": re.compile(r'<div class="stat">\s*<span>'),
    "seen": ">seen<",
    "battery": ">battery<",
    "actions": 'class="card-actions"',
}


def _shows(html: str, key: str) -> bool:
    marker = MARKERS[key]
    return bool(marker.search(html)) if hasattr(marker, "search") else marker in html


def _wake(app, *, battery: bool = True) -> None:
    """Make `kobo` a green node with a charge, so every field has something to draw.

    Neither fixture device declares `battery:` -- there is no portable way to ask, so a
    node that has not been told where to look reports nothing. Without this a test that
    ticks the battery off would be asserting against a battery that was never there.
    """
    lib = app.state.lib
    now = time.time()
    lib.probe._slot("kobo").reach = Reachability(
        state="online", last_ok=now, checked_at=now
    )
    if battery:
        device = lib.devices.config.by_id["kobo"]
        object.__setattr__(device, "battery", "/sys/class/power_supply/battery/capacity")
        lib.probe._slot("kobo").battery = Battery(percent=64, checked_at=now)


def _card(html: str, device_id: str = "kobo") -> str:
    """One card out of the grid.

    The fixture has two devices and only `kobo` is woken, so asserting across the whole
    grid asks about the other one as well -- and `phone` is red, which means it keeps the
    action row whatever the tick says. That is correct behaviour reading as a failure.
    """
    parts = html.split('<div class="card ')
    for part in parts:
        if f'id="card-{device_id}"' in part:
            return part
    raise AssertionError(f"no card for {device_id} in:\n{html}")


async def _grid(client, hidden: str | None = None, device_id: str = "kobo") -> str:
    cookies = {CARD_COOKIE: hidden} if hidden is not None else {}
    r = await client.get("/devices/grid", cookies=cookies)
    return _card(r.text, device_id)


# ------------------------------------------------------------------ defaults --


async def test_a_browser_that_never_visited_settings_sees_the_whole_card(client, app):
    """No cookie means the card as it has always been. This is why the cookie names what
    is *hidden*: the absent case has to be the old behaviour, not an empty card."""
    _wake(app)
    html = await _grid(client)
    for key, _label, _note in CARD_FIELDS:
        assert _shows(html, key), key


async def test_the_ticks_hide_exactly_what_they_name(client, app):
    """One at a time, because the interesting failure is a tick that takes a neighbour
    with it -- the storage line and the last-sync age share a `.stat`, and the bar sits
    outside it."""
    _wake(app)
    for key, _label, _note in CARD_FIELDS:
        html = await _grid(client, key)
        assert not _shows(html, key), f"{key} was still drawn"
        for other, _l, _n in CARD_FIELDS:
            if other != key:
                assert _shows(html, other), f"hiding {key} also took {other}"


async def test_an_unknown_key_hides_nothing(client, app):
    """The cookie is written from a form and can be hand-edited, so it is intersected
    with CARD_FIELDS rather than trusted -- `resolved_view` may trust its own only
    because nothing but an explicit ?view= ever writes it."""
    _wake(app)
    html = await _grid(client, "addr.banana..battery")
    assert not _shows(html, "addr") and not _shows(html, "battery")
    for key in ("target", "space", "sync", "seen", "actions"):
        assert _shows(html, key), key


async def test_everything_hidden_is_still_a_card(client, app):
    """The dot, the name and the badge are not tickable. Something has to say which
    device this is, and the root div's id is the target of the Test dialog's
    out-of-band swap."""
    _wake(app)
    html = await _grid(client, SEP.join(key for key, _l, _n in CARD_FIELDS))
    assert 'id="card-kobo"' in html
    assert "Test Kobo" in html
    assert 'class="dot dot-ok"' in html
    for key, _label, _note in CARD_FIELDS:
        assert not _shows(html, key), key


async def test_an_empty_group_leaves_no_empty_box(client, app):
    """`.card` is a flex column with a 10px gap, so a wrapper with nothing in it still
    costs a gap -- a hole where the stats used to be, and nothing on screen to explain
    it."""
    _wake(app)
    html = await _grid(client, "space.sync.seen.battery")
    assert 'class="card-stats"' not in html
    assert 'class="card-addr' in html, "the addr line is a different wrapper and stays"


# ----------------------------------------------------------- the two halves --


async def test_the_storage_line_survives_losing_either_half(client, app):
    """Last sync on the left, used/total on the right, one `.stat`. Each half goes on its
    own, and no label is invented for the other: a lone value is pushed right by CSS."""
    _wake(app)

    both = await _grid(client)
    assert _shows(both, "sync") and _shows(both, "space")

    no_sync = await _grid(client, "sync")
    assert 'class="stat"' in no_sync, "the storage figure lost the line it lives on"
    assert _shows(no_sync, "space")

    no_space = await _grid(client, "space")
    assert _shows(no_space, "sync")
    assert "track-disk" not in no_space, "the bar outlived the figure it draws"


# ------------------------------------------------------------------ actions --


async def test_the_tick_takes_the_buttons_off_every_card(client, app):
    """Whatever the dot says.

    This first exempted a red or a syncing card, on the reasoning that Retry (since folded
    into Test) was the only per-device re-probe in GRID and Abort the only way to stop a push. On this fleet six
    of ten nodes are red at any moment, so the tick left the buttons on most of the cards
    and read as doing nothing at all -- which is what it was reported as, on two different
    browsers. A preference that holds only for the cards you were not looking at is not a
    preference.
    """
    _wake(app)
    lib = app.state.lib

    green = await _grid(client, "actions")
    assert not _shows(green, "actions")
    assert "/device/kobo/test" not in green

    lib.probe._slot("kobo").reach = Reachability(
        state="offline", last_ok=None, checked_at=time.time(), error="timed out"
    )
    red = await _grid(client, "actions")
    assert not _shows(red, "actions"), "a red card kept the row the tick removed"
    assert "/device/kobo/test" not in red


async def test_a_syncing_card_drops_them_too_and_keeps_its_badge(client, app, monkeypatch):
    """The percentage is what a card still owes you while a job runs; Abort is in the
    dock, which is open whenever anything is running."""
    _wake(app)
    lib = app.state.lib
    job = SimpleNamespace(id="j1", device_id="kobo", state="running", pct=42.0)
    monkeypatch.setattr(lib.jobs, "active", lambda: [job])

    html = await _grid(client, "actions")
    assert not _shows(html, "actions")
    assert "/jobs/j1/abort" not in html
    assert "42%" in html, "the card stopped saying how far along it is"


def test_abort_is_still_reachable_with_the_buttons_off():
    """The claim the tick's note makes, checked rather than assumed: hiding the card's
    buttons moves Abort, it does not remove it."""
    from libnodes.templating import TEMPLATES_DIR

    dock = (TEMPLATES_DIR / "dock_card.html").read_text(encoding="utf-8")
    assert "/abort" in dock
    devices = (TEMPLATES_DIR / "devices.html").read_text(encoding="utf-8")
    assert "/devices/rescan" in devices


# ----------------------------------------------------------------- the page --


async def test_the_settings_page_offers_a_tick_for_every_field(client):
    """One list drives the page and the card. Two would drift, and the drift would show
    up as a tick that does nothing."""
    html = (await client.get("/settings")).text
    for key, label, _note in CARD_FIELDS:
        assert f'value="{key}"' in html, key
        assert label in html, label
    assert html.count('type="checkbox"') == len(CARD_FIELDS)


async def test_the_ticks_show_what_is_actually_saved(client):
    html = (await client.get("/settings", cookies={CARD_COOKIE: "addr.seen"})).text
    boxes = dict(re.findall(r'value="(\w+)"\s*\n?\s*([^>]*)>', html))
    assert "checked" not in boxes["addr"]
    assert "checked" not in boxes["seen"]
    assert "checked" in boxes["battery"]


async def test_opening_the_page_pins_nothing(client):
    """The rail link is a bare /settings, and a handler that cannot tell it from a Save
    would write whatever it happened to render -- which for a page of unchecked boxes is
    "hide everything". `/devices` documents the same rule from the far side."""
    r = await client.get("/settings")
    assert CARD_COOKIE not in r.cookies


async def test_saving_writes_the_cookie_and_the_grid_honours_it(client, app):
    _wake(app)
    r = await client.post(
        "/settings", data={"show": ["addr", "space", "actions"]}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings"
    # An unchecked box submits nothing, so what is stored is everything that did not.
    assert set(r.cookies[CARD_COOKIE].split(SEP)) == {"target", "sync", "seen", "battery"}

    # The jar carries it, so this is the round trip a browser makes.
    html = _card((await client.get("/devices/grid")).text)
    for key in ("addr", "space", "actions"):
        assert _shows(html, key), key
    for key in ("target", "sync", "seen", "battery"):
        assert not _shows(html, key), key


async def test_saving_nothing_hides_everything(client, app):
    """Every box unticked is a real answer, not a failed submission -- which is why only
    POST /settings writes this cookie, and a page render never does."""
    _wake(app)
    r = await client.post("/settings", follow_redirects=False)
    assert set(r.cookies[CARD_COOKIE].split(SEP)) == {k for k, _l, _n in CARD_FIELDS}


# ------------------------------------------------------------------- TABLE --


@pytest.mark.parametrize("hidden", ["", "addr.target.space.sync.seen.battery.actions"])
async def test_the_table_is_not_touched_by_any_of_this(client, app, hidden):
    """TABLE's CSS tracks, <thead> cells and row cells have to agree in number, so a
    hidden cell there is a four-way change and not a tick. The ticks are GRID-only and
    the Settings page says so; this is what keeps that true."""
    _wake(app)
    html = (await client.get("/devices/rows", cookies={CARD_COOKIE: hidden})).text
    for label in ("Device", "Type", "Address", "Target", "Storage",
                  "Battery", "Last seen", "Last sync", "Actions"):
        assert f'data-label="{label}"' in html, label


# ------------------------------------------------------------ every render --


async def test_every_path_that_draws_a_card_reads_the_preference(client, app, monkeypatch):
    """The card renders from more places than the grid, and the one that fails quietly is
    the Test dialog's out-of-band swap -- htmx drops a swap whose target is missing and
    says nothing. These two go through base_context alone, which is why the map lives
    there rather than in devices_context."""
    _wake(app)
    # libnodes_view too: the Test dialog carries a *row* unless this browser is in GRID,
    # which is itself a thing the card view gets right.
    cookies = {CARD_COOKIE: "addr", "libnodes_view": "grid"}

    one = (await client.get("/device/kobo/card", cookies=cookies)).text
    assert not _shows(one, "addr") and _shows(one, "target")

    # No ssh and no connect: only the card the dialog carries is under test here.
    import asyncio

    class _Proc:
        returncode = 255

        async def communicate(self):
            return (b"", b"")

    async def fake_exec(*a, **k):
        return _Proc()

    async def refused(*a, **k):
        raise ConnectionRefusedError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "open_connection", refused)

    tested = (await client.post("/device/kobo/test", cookies=cookies)).text
    assert 'id="card-kobo"' in tested, "the Test dialog stopped carrying a card"
    # The dialog above it echoes the ssh command, address and all; only the card counts.
    card = tested[tested.index('id="card-kobo"'):]
    assert not _shows(card, "addr")


def test_the_card_never_hides_anything_with_css():
    """`.card` is display:flex, which outranks both an .is-hidden class and the UA's own
    [hidden] -- the mistake CLAUDE.md records under asserting on computed style. The
    template omits the block instead, which is a thing the tests above can see."""
    from libnodes.templating import TEMPLATES_DIR

    text = (TEMPLATES_DIR / "device_card.html").read_text(encoding="utf-8")
    assert "display:none" not in text.replace(" ", "")
    assert "hidden" not in text.replace('card_show', ''), "a class crept in where an if belongs"
