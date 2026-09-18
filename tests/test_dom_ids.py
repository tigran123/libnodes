"""A device id has to survive being put in a CSS selector.

`sigmaai.au` is the id that found this. htmx spans two worlds with the same string:
`hx-target="#node-<id>"` is a querySelector, and an out-of-band swap builds its own
selector as `"#" + element.getAttribute("id")` and runs *that* through querySelectorAll.
A dot makes `#scan-status-sigmaai.au` parse as the id `scan-status-sigmaai` plus the class
`au`, which matches nothing — and htmx answers an unresolvable target by firing
htmx:targetError and **not sending the request at all**.

So "Scan device" on that node did nothing whatsoever, with no request in the access log to
say why, for as long as the node existed. Row Retry, card Retry (both since removed) and the
Test dialog's out-of-band row refresh were broken the same way and had never been pressed.

The fleet these tests otherwise use is all dot-free, which is exactly why nothing caught
it. This module keeps one node with a dot in its id.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: Anything outside this set needs escaping in a CSS id selector, and `Device.dom_id`
#: exists to make sure nothing outside it ever reaches one.
SAFE = re.compile(r"[A-Za-z0-9_-]+")


@pytest.fixture
def devices_file(settings) -> Path:
    """Overrides the shared fixture: this module needs an id that is not selector-safe."""
    path = settings.resolved_devices_file
    path.write_text(
        """
defaults:
  timeout: 20
  retries: 0

devices:
  - id: sigmaai.au
    name: Dotted Upstream
    abbr: SRC
    type: linux
    host: 127.0.0.1
    port: 22
    user: tigran
    target: /Books
    fs: ext4
    sync_mode: upstream

  - id: kobo
    name: Test Kobo
    abbr: TK
    type: kobo
    host: 127.0.0.1
    port: 2222
    user: root
    target: /mnt/onboard/Books
    full_sync: true
""".lstrip(),
        encoding="utf-8",
    )
    return path


def test_a_dotted_id_is_folded_for_the_dom_and_left_alone_everywhere_else(app):
    """`dom_id` is a presentation concern and must not leak into the device's identity.

    The raw id is the key in manifests.db, jobs.db and probe.json, and it is the hostname.
    Renaming the node to suit CSS would orphan its history to fix a stylesheet problem.
    """
    device = app.state.lib.devices.config.by_id["sigmaai.au"]
    assert device.id == "sigmaai.au"
    assert device.dom_id == "sigmaai-au"
    assert SAFE.fullmatch(device.dom_id)
    # A dot-free id is its own dom_id, so nothing in the existing fleet moves.
    assert app.state.lib.devices.config.by_id["kobo"].dom_id == "kobo"


@pytest.mark.parametrize(
    "url", ["/devices/rows", "/devices/grid", "/device/sigmaai.au/menu"]
)
async def test_every_id_the_page_renders_is_safe_to_select(client, url):
    """Because htmx will build a selector out of it whether or not we meant it to."""
    html = (await client.get(url)).text
    for rendered in re.findall(r'\bid="([^"]+)"', html):
        assert SAFE.fullmatch(rendered), (
            f"{url} renders id={rendered!r}, which CSS cannot select unescaped — "
            "an out-of-band swap aimed at it is dropped in silence"
        )


@pytest.mark.parametrize(
    "url", ["/devices/rows", "/devices/grid", "/device/sigmaai.au/menu"]
)
async def test_every_target_names_an_element_that_exists(client, url):
    """The assertion htmx makes at runtime, made here instead.

    An `hx-target` that resolves to nothing is not a cosmetic fault: the request is never
    sent. Checked by id rather than by running a selector engine, which is the same thing
    for the `#name` form every one of these uses.
    """
    html = (await client.get(url)).text
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    targets = [t for t in re.findall(r'hx-target="([^"]+)"', html) if t.startswith("#")]
    assert targets, f"{url} renders no id targets — this test has stopped testing anything"
    for target in targets:
        name = target[1:]
        # Targets into the shell (#notices, #dock) are rendered by base.html, not here.
        if name in {"notices", "dock", "device-menu", "device-extras", "lib"}:
            continue
        assert name in ids, (
            f"{url} points hx-target at {target!r}, which nothing on the page answers to"
        )


async def test_the_urls_still_carry_the_real_id(client):
    """Only the DOM name is folded. The routes are keyed by the device's actual id."""
    menu = (await client.get("/device/sigmaai.au/menu")).text
    assert "/device/sigmaai.au/scan" in menu
    assert "/device/sigmaai-au/scan" not in menu


def test_two_ids_that_collapse_to_one_dom_id_are_refused(settings):
    """Vanishingly unlikely, and it would put two devices in one row of the DOM."""
    from libnodes.models import parse_devices

    _, issues = parse_devices(
        """
devices:
  - id: sigmaai.au
    name: A
    host: h
    target: /Books
  - id: sigmaai-au
    name: B
    host: h
    target: /Books
""".lstrip()
    )
    assert issues, "two ids folding to one dom_id should reach the validation strip"
    assert any("sigmaai-au" in i.message for i in issues)
