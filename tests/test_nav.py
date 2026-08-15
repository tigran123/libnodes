"""Exactly one nav item is highlighted at a time.

Two views once shared the `active` key "devices", so opening either lit up both Devices
and devices.yaml. The keys are strings, so nothing catches a collision but a test.
"""

from __future__ import annotations

import re

import pytest

# path -> the nav label that should be highlighted
PAGES = {
    "/devices": "Devices",
    "/library": "Library",
    "/jobs": "Jobs",
    "/presets": "Presets",
    "/devices.yaml": "devices.yaml",
    "/keys": "Keys",
}

NAV_ITEM = re.compile(
    r'<a class="nav-item([^"]*)"[^>]*href="([^"]+)"[^>]*>\s*([^<\s][^<]*?)\s*(?:<|$)',
    re.S,
)


def _active_labels(html: str) -> list[str]:
    out = []
    for classes, _href, label in NAV_ITEM.findall(html):
        if "is-active" in classes:
            out.append(label.strip())
    return out


@pytest.mark.parametrize("path,expected", PAGES.items())
async def test_exactly_one_nav_item_is_active(client, path, expected):
    r = await client.get(path)
    assert r.status_code == 200
    active = _active_labels(r.text)
    assert active == [expected], f"{path} highlighted {active}"


async def test_devices_and_devices_yaml_do_not_share_a_key(client):
    """The specific regression: these two are adjacent and easy to conflate."""
    devices = _active_labels((await client.get("/devices")).text)
    config = _active_labels((await client.get("/devices.yaml")).text)

    assert devices == ["Devices"]
    assert config == ["devices.yaml"]
    assert set(devices).isdisjoint(config)


async def test_every_nav_target_actually_resolves(client):
    """A highlighted item that 404s is worse than one that never highlights."""
    for path in PAGES:
        assert (await client.get(path)).status_code == 200, path


# ----------------------------------------------------- pre-rename URLs --

# A page loaded before "Fleet" became "Devices" keeps polling the old URLs for as long
# as it stays open. Left to 404 it just stops updating, silently and permanently.
OLD_TO_NEW = [
    ("/fleet", "/devices"),
    ("/fleet/rows", "/devices/rows"),
    ("/fleet/status", "/devices/status"),
    ("/fleet/grid", "/devices/grid"),
    ("/node/kobo/row", "/device/kobo/row"),
]


@pytest.mark.parametrize("old,new", OLD_TO_NEW)
async def test_old_urls_redirect(client, old, new):
    r = await client.get(old, follow_redirects=False)
    assert r.status_code == 308, old
    assert r.headers["location"] == new


async def test_old_urls_keep_their_query_string(client):
    r = await client.get("/fleet/rows?q=kobo", follow_redirects=False)
    assert r.headers["location"] == "/devices/rows?q=kobo"


async def test_a_stale_page_recovers_by_following_the_redirect(client):
    """End to end: the old polling URL returns real rows again."""
    r = await client.get("/fleet/rows", follow_redirects=True)
    assert r.status_code == 200
    assert "Test Kobo" in r.text


async def test_old_post_actions_redirect_without_losing_the_method(client):
    """308, not 302 — a POST must not silently become a GET."""
    r = await client.post("/node/kobo/probe", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "/device/kobo/probe"
