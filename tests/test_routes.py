"""Route behaviour, and the HTMX contract every fragment has to honour."""

from __future__ import annotations

import re
from urllib.parse import quote, unquote

import pytest

from libnodes.templating import TEMPLATES_DIR

FRAGMENTS = [
    "/devices/rows",
    "/devices/grid",
    "/devices/status",
    "/device/kobo/card",
    "/lib/pane",
    "/lib/list",
    "/lib/selection",
    "/lib/index-status",
    "/jobs/rows",
    "/jobs/dock",
    "/jobs/telemetry",
]

PAGES = ["/devices", "/library", "/jobs", "/settings"]


async def test_root_redirects_to_devices(client):
    r = await client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/devices"


@pytest.mark.parametrize("path", PAGES)
async def test_pages_render(client, path):
    r = await client.get(path)
    assert r.status_code == 200
    assert "<!doctype html>" in r.text.lower()


@pytest.mark.parametrize("path", FRAGMENTS)
async def test_fragments_render_standalone(client, path):
    """The whole HTMX contract: a fragment must not depend on the page shell.

    If any of these ever emit `<html>`, swapping them into a live page would nest a
    document inside a div.
    """
    r = await client.get(path)
    assert r.status_code == 200
    assert "<html" not in r.text.lower()
    assert "<!doctype" not in r.text.lower()


#: The five verbs that make htmx issue a request. `hx-on`, `hx-target` and friends do not.
HX_REQUEST_ATTRS = ("hx-get=", "hx-post=", "hx-put=", "hx-patch=", "hx-delete=")


def test_no_button_removes_itself_while_asking_for_something():
    """A button may not both issue a request and tear its own dialog out in `onclick`.

    An inline handler is registered when the fragment is parsed and htmx's when it
    processes the node, so `onclick` always runs first. htmx 2 then drops the request
    without a sound — `getRootNode() === document` is false for a detached element, and it
    is checked both in the trigger handler and again in issueAjaxRequest. The picker's
    Push button did nothing at all: no request, no error, the dialog just vanished. Close
    with `hx-on::after-request` instead, which is also the only order under which the
    reply is visible (.backdrop is z-index 60, .notices 50).
    """
    offenders = []
    for template in sorted(TEMPLATES_DIR.rglob("*.html")):
        for tag in re.findall(r"<button\b[^>]*>", template.read_text(encoding="utf-8")):
            if "onclick=" not in tag or "remove(" not in tag:
                continue
            if any(attr in tag for attr in HX_REQUEST_ATTRS):
                offenders.append(f"{template.relative_to(TEMPLATES_DIR)}: {tag}")
    assert not offenders, "\n".join(offenders)


async def test_devices_view_lists_configured_devices(client):
    r = await client.get("/devices/rows")
    assert "Test Kobo" in r.text
    assert "Test Phone" in r.text
    assert "/mnt/onboard/Books" in r.text


async def test_devices_filter(client):
    r = await client.get("/devices/rows", params={"q": "phone"})
    assert "Test Phone" in r.text
    assert "Test Kobo" not in r.text


# ------------------------------------------------- the remembered devices layout --
#
# The toggle is a link, so the choice lived only in the query string, and base.html's
# rail points at a bare /devices: picking GRID and walking to Library came back to TABLE.
# A cookie carries it, and these pin both that and the fragments that have to agree with
# the branch on screen -- #device-rows is the cards container in grid mode, so a table-only
# fragment aimed at it replaced every card with rows.


async def test_the_devices_view_survives_a_trip_to_the_library(client):
    chosen = await client.get("/devices", params={"view": "grid"})
    assert chosen.cookies["libnodes_view"] == "grid"

    await client.get("/library")
    back = await client.get("/devices")  # the rail link: no query at all
    assert 'hx-get="/devices/grid"' in back.text
    assert 'hx-get="/devices/rows"' not in back.text


async def test_a_bare_devices_page_still_defaults_to_table(client):
    r = await client.get("/devices")
    assert 'hx-get="/devices/rows"' in r.text
    assert 'hx-get="/devices/grid"' not in r.text


async def test_a_bare_devices_page_does_not_pin_its_own_default(client):
    """Arriving by the rail must not write the preference it just guessed.

    With `view: str = "table"` the handler could not tell the rail link from a click on
    TABLE, so the first bare /devices would have frozen TABLE in the cookie for good.
    """
    r = await client.get("/devices")
    assert "libnodes_view" not in r.cookies
    assert "libnodes_view" not in r.headers.get("set-cookie", "")


async def test_an_unknown_view_falls_back_without_writing_a_cookie(client):
    r = await client.get("/devices", params={"view": "nonsense"})
    assert 'hx-get="/devices/rows"' in r.text
    assert "libnodes_view" not in r.headers.get("set-cookie", "")


# ------------------------------------------------ the remembered library position --
#
# The same bug as the block above, from the other page: the rail's Library link is a bare
# /library, so walking to Devices and back landed at /Books however deep you had been.
# The cookie fills in the *link*; it never reinterprets a bare /library, so a typed URL,
# a bookmark and the Back button all still mean exactly what they say.


def _library_link(html: str) -> str:
    """The rail's Library href, which is the whole feature."""
    m = re.search(r'<a class="nav-item[^"]*"\s+href="([^"]*)">Library</a>', html)
    assert m, "no Library link in the rail"
    return m.group(1)


async def test_the_library_position_survives_a_trip_to_the_devices_page(client):
    await client.get("/lib/pane", params={"p": "Science/Physics"})
    back = await client.get("/devices")  # the rail link: no query at all
    assert _library_link(back.text) == "/library?p=Science%2FPhysics"


async def test_going_back_to_the_root_is_remembered_too(client):
    """The breadcrumb's root link is a bare /lib/pane, and it must not be the one
    navigation the memory ignores — otherwise deliberately going up to /Books and walking
    away brings you back down again."""
    await client.get("/lib/pane", params={"p": "Science/Physics"})
    await client.get("/lib/pane")
    back = await client.get("/jobs")
    assert _library_link(back.text) == "/library"


async def test_the_library_page_points_its_own_rail_at_itself(client):
    """base_context reads the cookie, which on this request is still one navigation
    behind. A rail link aimed somewhere other than the page on screen is the "the URL
    moved and the content did not" failure again."""
    await client.get("/lib/pane", params={"p": "Fiction"})
    here = await client.get("/library", params={"p": "Science"})
    assert _library_link(here.text) == "/library?p=Science"


@pytest.mark.parametrize("stale", ["Science/Gone", "../etc", ".data", "urantia-library"])
async def test_a_remembered_directory_that_no_longer_exists_is_forgotten(client, stale):
    """The one thing `resolved_view` never has to do: "grid" cannot go stale and a path
    can. `index.require` answers a renamed, deleted or infrastructure path with a 400, so
    an unvalidated cookie would break the rail link itself."""
    client.cookies.set("libnodes_lib_path", quote(stale, safe=""))
    r = await client.get("/devices")
    assert r.status_code == 200
    assert _library_link(r.text) == "/library"


async def test_a_file_is_not_a_position(client):
    """`p` lists a directory. A cookie naming a book would render its parent's listing
    under a breadcrumb claiming otherwise."""
    client.cookies.set(
        "libnodes_lib_path", quote("Science/Physics/Feynman.djvu", safe="")
    )
    assert _library_link((await client.get("/devices")).text) == "/library"


async def test_a_path_that_is_not_a_cookie_value_still_survives(app):
    """A comma, a space and a Cyrillic name — none of which is a cookie-octet, and all of
    which exist in a real /Books. Unencoded, http.cookies quotes and escapes the whole
    value and it comes back unsplittable: the bug `cardprefs.SEP` records, which only a
    test ever caught. The fixture library is ASCII, so this is checked directly."""
    from fastapi.responses import Response

    from libnodes.libpos import POS_COOKIE, remember

    for path in ("Fiction/Perov, L/Book 1965", "Художественная/Перов"):
        response = Response()
        remember(response, path)
        header = response.headers["set-cookie"]
        assert "," not in header.split(";")[0], "a comma reached the cookie value"

        sent = header.split(";")[0].split("=", 1)[1]
        assert unquote(sent) == path


async def test_a_grid_page_keeps_its_cards_when_filtered_or_rescanned(client):
    client.cookies.set("libnodes_view", "grid")

    page = await client.get("/devices")
    assert 'hx-get="/devices/grid"' in page.text, "the filter box still names the table"

    rescan = await client.post("/devices/rescan")
    assert 'class="cards"' in rescan.text
    assert 'class="trow' not in rescan.text
    # ...and the follow-up that collects the sweep has to come back as cards too.
    assert 'hx-get="/devices/grid"' in rescan.text


@pytest.fixture
def quiet_test(monkeypatch):
    """Press Test without spending an ssh or a connect: the dialog's own output is not
    what these assert on, only the device it carries out of band."""
    import asyncio

    class _Proc:
        returncode = 255

        async def communicate(self):
            return (b"", b"ssh: connect to host kobo: No route to host\n")

    async def fake_exec(*a, **k):
        return _Proc()

    async def refused(*a, **k):
        raise ConnectionRefusedError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "open_connection", refused)


async def test_a_test_in_grid_refreshes_one_card(client, quiet_test):
    """Test is the only per-device re-probe since Retry went, so its out-of-band refresh
    is what repaints a card. Aimed at a #node-<id> that grid mode does not render, htmx
    drops the swap silently and the card keeps the reading the test just contradicted."""
    client.cookies.set("libnodes_view", "grid")
    r = await client.post("/device/kobo/test")
    assert 'id="card-kobo"' in r.text
    assert 'id="node-kobo"' not in r.text


async def test_a_test_in_table_refreshes_one_row(client, quiet_test):
    r = await client.post("/device/kobo/test")
    assert 'id="node-kobo"' in r.text
    assert 'id="card-kobo"' not in r.text


def test_there_is_no_retry_beside_test():
    """Retry was a TCP connect and a re-render, and Test does both before its ssh -- so a
    red row carried two buttons for one job. The Actions tooltip names Test instead."""
    for name in ("device_row.html", "device_card.html"):
        text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        assert ">Retry<" not in text, name
        assert "/probe\"" not in text, name
        assert "Retry first" not in text, name


async def test_switching_view_keeps_the_filter(client):
    r = await client.get("/devices", params={"q": "phone", "view": "table"})
    assert "/devices?view=grid&amp;q=phone" in r.text


@pytest.mark.parametrize("view,fragment", [("table", "rows"), ("grid", "grid")])
async def test_the_ten_second_poll_carries_the_filter(client, view, fragment):
    """The poll repaints the element the filter writes to, so without hx-include it
    erases it. A bare GET binds `q` to None, `_filtered` short-circuits and innerHTML puts
    the whole fleet back under a box still reading `lg` — every ten seconds, for ever,
    since innerHTML leaves the polling div and its trigger intact.

    hx-disinherit ships with it: hx-include is inherited and every row's Test/Abort
    button is inside this container. That is the bug #sel-form carries one for.
    """
    r = await client.get("/devices", params={"view": view})
    div = r.text.split('<div id="device-rows"')[1].split(">")[0]
    assert f'hx-get="/devices/{fragment}"' in div
    assert 'hx-include="[name=q]"' in div
    assert 'hx-disinherit="hx-include"' in div


async def test_rescan_keeps_the_filter_too(client):
    """`devices_rescan` has always declared `q`; nothing was sending it."""
    button = (await client.get("/devices")).text
    button = button.split('hx-post="/devices/rescan"')[1].split(">")[0]
    assert 'hx-include="[name=q]"' in button


def _subtitle(text: str) -> tuple[int, int]:
    """The titlebar subtitle's two numbers, wherever it was rendered."""
    m = re.search(r"(\d+) devices · (\d+) profiles", text)
    assert m, "no subtitle in this response"
    return int(m.group(1)), int(m.group(2))


async def test_the_subtitle_rides_the_status_poll(client):
    """The subtitle sits in .titlebar, outside #device-rows and outside #device-status,
    so nothing on the page repainted it: edit devices.yaml and it stayed one device short
    until a reload, beside a chip and a set of cards that had both already corrected
    themselves. The 10s status poll carries it out of band rather than earning a third
    request of its own."""
    r = await client.get("/devices/status")
    span = r.text.split('id="device-meta"')[1].split(">")[0]
    assert 'hx-swap-oob="true"' in span
    assert _subtitle(r.text)[0] == 2


async def test_the_subtitle_and_the_chip_count_the_same_fleet(client):
    """Both halves come from `probe.reachable_count`, so the sentence and the chip beside
    it can never disagree about how big the fleet is — which is the whole point of putting
    the subtitle on this response rather than on the filtered rows one."""
    text = (await client.get("/devices/status")).text
    devices, _profiles = _subtitle(text)
    reachable = re.search(r"\d+/(\d+) online", text)
    assert reachable and int(reachable.group(1)) == devices


async def test_the_subtitle_is_a_fleet_count_not_a_filtered_one(client):
    """`profiles` is fleet-wide by construction — distinct types across every device — so
    a filtered count beside it made one sentence count two populations: a filter down to
    the Kobo read "1 devices · 2 profiles" when that one device is one type. The table
    narrows; the subtitle does not."""
    r = await client.get("/devices", params={"q": "kobo"})
    assert _subtitle(r.text) == (2, 2)
    assert "Test Phone" not in r.text


async def test_the_page_renders_one_subtitle(client):
    """devices.html includes device_status.html inline in its topbar, so without the
    `{% if oob %}` guard the page would carry two id="device-meta" spans and htmx would
    repaint whichever it found first."""
    assert (await client.get("/devices")).text.count('id="device-meta"') == 1

    # `data`, not `params`: htmx puts hx-include values in the body of a POST, and a
    # query-parameter handler binds None there however the button is wired.
    swept = await client.post("/devices/rescan", data={"q": "phone"})
    assert "Test Phone" in swept.text
    assert "Test Kobo" not in swept.text
    # ...including the follow-up that collects the sweep 2.5s later.
    assert 'hx-get="/devices/rows?q=phone"' in swept.text


async def test_library_lists_real_entries(client):
    r = await client.get("/lib/list", params={"p": "Science/Physics"})
    assert "Feynman.djvu" in r.text
    assert "Landau.pdf" in r.text


async def test_library_filter_reports_counts(client):
    r = await client.get("/lib/list", params={"p": "Science/Physics", "q": "Feynman"})
    assert "Feynman.djvu" in r.text
    assert "matches" in r.text


async def test_the_filter_narrows_the_listing_rather_than_leaving_it(client):
    """Typing in the box narrows what is on screen; it does not start a search.

    The recursive version answered the root with a flat list of basenames from anywhere in
    the library — and could not do this, which is what the box in front of a directory
    listing is for.
    """
    r = await client.get("/lib/list", params={"p": "", "q": "sci"})
    assert "Science" in r.text
    assert "Fiction" not in r.text
    # Two levels down, and therefore not an answer to a question about this level.
    assert "Feynman.djvu" not in (
        await client.get("/lib/list", params={"p": "", "q": "Feynman"})
    ).text


async def test_library_rejects_traversal(client):
    r = await client.get("/lib/list", params={"p": "../../etc"})
    assert r.status_code == 400


async def test_library_rejects_infrastructure_paths(client):
    """`.data` is inside the library root but must never be browsable."""
    for path in (".data", "urantia-library"):
        assert (await client.get("/lib/list", params={"p": path})).status_code == 400


async def test_selection_bar_appears_only_with_a_selection(client):
    empty = await client.get("/lib/selection")
    assert empty.text.strip() == ""

    picked = await client.get(
        "/lib/selection", params={"path": ["Science/Physics/Landau.pdf"]}
    )
    assert "1 item selected" in picked.text


@pytest.mark.parametrize("name", ["app.js", "app.css", "htmx.min.js", "fonts.css"])
async def test_static_urls_change_when_the_file_does(client, name):
    """StaticFiles sends no Cache-Control, so a browser caches these by heuristic
    freshness — ~10% of the file's age — and does not revalidate. A select-all deployed
    at 12:26 was still running the previous day's app.js at 12:34: new markup, old
    script, indistinguishable from a fix that did not work."""
    r = await client.get("/library")
    assert f"/static/{name}?v=" in r.text, f"{name} is linked without a cache stamp"


async def test_the_asset_stamp_follows_the_file(tmp_path, monkeypatch):
    """The stamp has to be derived from the file, or it is decoration."""
    from libnodes import templating

    target = tmp_path / "app.js"
    target.write_text("//")
    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)

    first = templating.asset("app.js")
    import os

    os.utime(target, (0, 0))
    assert templating.asset("app.js") != first


async def test_the_header_offers_a_select_all(client):
    """Dry-running the whole library meant ticking all 18 top-level directories by hand."""
    r = await client.get("/lib/pane")
    head = r.text.split('<div id="file-rows">')[0]
    assert "data-select-all" in head, "no select-all in the table header"


async def test_the_select_all_box_is_not_part_of_the_selection(client):
    """It sits inside #sel-form, and the selection bar is only an hx-include of the
    checked boxes there — a name on it would post a phantom path with every push."""
    r = await client.get("/lib/pane")
    head = r.text.split('<div id="file-rows">')[0]
    box = head[head.index("data-select-all") - 200 : head.index("data-select-all") + 200]
    assert "name=" not in box, f"select-all is serialised into the selection: {box}"


async def test_select_all_is_wired_to_the_rows_the_table_is_showing(client):
    """The JS pairs `[data-select-all]` with the `.trow input.check` boxes inside the
    enclosing `[data-selectable]`. All three have to be present for it to do anything."""
    r = await client.get("/lib/pane")
    assert "data-selectable" in r.text
    assert 'class="trow file-grid"' in r.text
    assert 'class="check" type="checkbox" name="path"' in r.text


async def test_any_format_can_be_pushed_to_any_device(client):
    """No format gating: a .djvu to a device that never declared djvu is fine."""
    r = await client.get(
        "/lib/selection", params={"path": ["Science/Physics/Feynman.djvu"]}
    )
    assert "1 item selected" in r.text
    assert "does not list" not in r.text


async def test_push_to_an_offline_node_asks_first(client, app):
    """The device is unreachable in tests, so the one blocking dialog comes back.

    Nothing is queued until the user confirms — see tests/test_job_lifecycle.py.
    """
    lib = app.state.lib
    r = await client.post(
        "/jobs", data={"device": "kobo", "path": "Science/Physics/Landau.pdf"}
    )
    assert r.status_code == 200
    assert "is unreachable" in r.text
    assert lib.jobs.recent() == []


async def test_push_to_unknown_device_is_an_error_toast(client):
    r = await client.post("/jobs", data={"device": "nope", "path": "Fiction"})
    assert "Could not queue" in r.text
    assert "no device selected" in r.text


async def test_push_with_nothing_selected_is_rejected(client):
    r = await client.post("/jobs", data={"device": "kobo"})
    assert "nothing selected" in r.text


async def test_push_rejects_paths_outside_the_index(client, app):
    """Traversal is refused outright, before anything reaches the queue."""
    r = await client.post("/jobs", data={"device": "kobo", "path": "../../etc/passwd"})
    assert r.status_code == 400
    assert app.state.lib.jobs.recent() == []


async def test_push_rejects_unindexed_but_real_paths(client, app):
    """`urantia-library/secrets.env` exists on disk; it is still not pushable."""
    r = await client.post(
        "/jobs", data={"device": "kobo", "path": "urantia-library/secrets.env"}
    )
    assert "nothing selected" in r.text
    assert app.state.lib.jobs.recent() == []


async def test_config_edits_apply_without_any_button(client, devices_file):
    """The mtime watcher is the real mechanism — no reload action is involved.

    It is also all that is left of the devices.yaml view: that page could only ever show
    the file, which is why it went, and this is the half that mattered.
    """
    assert "Renamed Kobo" not in (await client.get("/devices/rows")).text
    devices_file.write_text(
        devices_file.read_text().replace("name: Test Kobo", "name: Renamed Kobo")
    )
    assert "Renamed Kobo" in (await client.get("/devices/rows")).text


async def test_a_broken_devices_yaml_says_so_in_the_top_bar(client, devices_file):
    """The chip is the only surface those errors have now.

    Without it a typo is perfectly silent: the store keeps serving the last good config,
    so every device is still listed, still probed, still pushable — and the edit that was
    just saved has simply not happened.
    """
    clean = await client.get("/devices/status")
    assert "problem" not in clean.text

    devices_file.write_text(
        devices_file.read_text().replace("port: 2222", 'port: "2222 "')
    )
    bad = await client.get("/devices/status")
    assert "1 problem in devices.yaml" in bad.text
    assert "devices[0].port" in bad.text
    assert "dot-err" in bad.text

    # And it clears itself: same poll, no action to take.
    devices_file.write_text(
        devices_file.read_text().replace('port: "2222 "', "port: 2222")
    )
    assert "problem" not in (await client.get("/devices/status")).text


async def test_healthz(client):
    r = await client.get("/healthz")
    body = r.json()
    assert body["ok"] is True
    assert body["index"]["ready"] is True
    assert body["devices"]["total"] == 2


async def test_missing_log_is_404(client):
    assert (await client.get("/jobs/9999/log")).status_code == 404


# ------------------------------- library: the table is the navigator --


async def test_a_directory_row_is_a_link_and_a_file_row_is_not(client):
    """Below 972px the tree pane was `display: none` with nothing in its place, so a
    directory row could be ticked and pushed but never entered — a book three levels down
    was unreachable on a tablet. The name is the way in; the rest of the row still ticks.
    """
    r = await client.get("/lib/pane")
    rows = r.text.split('<div id="file-rows">')[1]

    assert '<a class="file-name"' in rows, "no directory is enterable from the table"
    for attr in (
        'hx-get="/lib/pane?p=Science"',
        'hx-target="#lib"',
        'hx-swap="outerHTML"',
        'hx-push-url="/library?p=Science"',
        'href="/library?p=Science"',   # the half that works with no JS at all
    ):
        assert attr in rows, f"the directory link is missing {attr}"

    deeper = await client.get("/lib/pane", params={"p": "Science/Physics"})
    files = deeper.text.split('<div id="file-rows">')[1]
    assert '<span class="file-name" title="Science/Physics/Feynman.djvu">' in files
    assert "Feynman.djvu</a>" not in files, "a book is not a directory to walk into"


async def test_a_directory_link_carries_only_where_it_is_going(client):
    """It drops q and sort, and now it has to say so on purpose.

    The filter used to make this vacuous — `children()` appended `is_dir = 0` for a query,
    so a directory row could not coexist with one. It can now, and dropping `q` is what
    clears the box: the link swaps the whole #lib pane, which comes back with an empty
    filter and the full listing, exactly as a tree click did.
    """
    r = await client.get("/lib/pane", params={"q": "sci", "sort": "size"})
    rows = r.text.split('<div id="file-rows">')[1]
    assert '<a class="file-name" title="Science"' in rows, "the match is not a way in"
    assert 'hx-get="/lib/pane?p=Science"' in rows
    for leaked in ("p=Science&", "q=sci", "sort=size"):
        assert leaked not in rows, f"the directory link smuggled {leaked}"


async def test_the_library_has_no_format_filter(client):
    """EPUB/PDF/DJVU chips were a hardcoded three-item list that took a whole row of the
    filterbar on a phone (210 px of bar, 178 without them, measured at 412 px) and hid
    every directory whenever one was on, NULL satisfying no `fmt IN (...)`. They went
    end to end, so no hx-include may still ask for them either."""
    for route in ("/library", "/lib/pane"):
        r = await client.get(route)
        assert 'name="fmt"' not in r.text
        assert "[name=fmt]" not in r.text


async def test_the_breadcrumb_is_one_link_per_ancestor_plus_a_root(client):
    """The only way back up. `ancestors` was already in every library context and had
    only ever been concatenated into an inert span."""
    r = await client.get("/lib/pane", params={"p": "Science/Physics"})
    crumb = r.text.split('class="pathline"')[1].split("</nav>")[0]

    assert crumb.count("<a ") == 2, f"expected root + Science, got:\n{crumb}"
    assert 'hx-get="/lib/pane"' in crumb, "no link back to the library root"
    assert 'hx-get="/lib/pane?p=Science"' in crumb
    # Where you already are is not a control.
    assert 'hx-get="/lib/pane?p=Science/Physics"' not in crumb
    assert 'aria-current="page"' in crumb and ">Physics<" in crumb

    root = await client.get("/lib/pane")
    top = root.text.split('class="pathline"')[1].split("</nav>")[0]
    assert "<a " not in top, "the root offers a link to itself"
    assert 'class="leaf"' in top


async def test_rescan_is_the_one_reindex_control(client, app, monkeypatch):
    """The Library's ⟳ folded into Rescan on Devices. It was the only manual reindex
    control in the app, so it may go only because this button now does its job -- a book
    copied into /Books by hand would otherwise wait up to reindex_interval to appear."""
    calls = []
    monkeypatch.setattr(app.state.lib, "reindex_soon", lambda: calls.append(1))
    assert (await client.post("/devices/rescan")).status_code == 200
    assert calls == [1]

    devices = (await client.get("/devices")).text
    start = devices.index('hx-post="/devices/rescan"')
    button = devices[devices.rindex("<button", 0, start) : devices.index("</button>", start)]
    assert "<svg" in button and 'title="Rescan:' in button

    pane = (await client.get("/lib/pane")).text
    assert "/lib/reindex" not in pane and "⟳" not in pane
    assert (await client.post("/lib/reindex")).status_code in (404, 405)


async def test_the_index_chip_polls_only_while_a_rebuild_runs(client, app, monkeypatch):
    """A rebuild can now start from another page, so the chip carries its own poll while
    one runs, and the swap that reports it finished removes the poll with it."""
    index = app.state.lib.index
    monkeypatch.setattr(index, "_running", True)
    running = (await client.get("/lib/index-status")).text
    assert 'hx-get="/lib/index-status"' in running and "every 2s" in running

    monkeypatch.setattr(index, "_running", False)
    done = (await client.get("/lib/index-status")).text
    assert "hx-get" not in done and "entries" in done


async def test_the_pane_still_guards_the_paths_the_tree_route_used_to(client):
    assert (await client.get("/lib/pane", params={"p": "../etc"})).status_code == 400
    assert (await client.get("/lib/pane", params={"p": ".data"})).status_code == 400


async def test_the_table_does_not_smuggle_its_own_directory_into_a_link(client):
    """hx-include is inherited, and #sel-form carries `hx-include="#lib-params"` — that is
    `p=<the directory we are in>`. Without hx-disinherit every link inside the table
    appended it, so `hx-get="/lib/pane?p=Science/Aviation"` went out as
    `?p=Science/Aviation&p=Science`; FastAPI binds the last value, so the server answered
    with the directory you were already in while hx-push-url had already written the new
    one to the address bar. The URL moved and the content did not, and nothing failed.
    """
    r = await client.get("/lib/pane")
    form = r.text.split('<form id="sel-form"')[1].split(">")[0]
    assert 'hx-disinherit="hx-include"' in form
    # The form's own include is what the selection bar is built from and must survive.
    assert 'hx-include="#lib-params"' in form


async def test_the_index_and_the_fleet_state_their_age_the_same_way(client):
    """Rescan now moves both, so the two ages share one vocabulary and each names its
    subject. They are still two clocks -- the probe ticks every ~10 s by itself -- so this
    pins the words, not the numbers."""
    library = (await client.get("/library")).text
    devices = (await client.get("/devices")).text
    assert "fresh " not in library
    assert "last scan" not in devices
    assert "devices checked " in devices
    assert re.search(r"index (\d+[smhd] ago|just now|yesterday|never)", library)


async def test_the_fleet_chip_counts_green_dots_in_plain_words(client):
    """`online` because the figure is `Reachability.online` -- the green dots, not the
    amber ones -- and no "sshd" prefix, which cost a phone a third of what it could show."""
    status = (await client.get("/devices/status")).text
    assert re.search(r"\d+/\d+ online", status)
    assert "sshd" not in status and "reachable" not in status
    assert 'class="chip chip-checked"' in status


def _files_cell(html: str, job_id: int) -> str:
    """One job's FILES cell, so an assertion cannot match a neighbour's row -- or the
    em-dashes the Started and DONE cells draw in the same row for unrelated reasons."""
    start = html.index(f'id="job-row-{job_id}"')
    nxt = html.find('id="job-row-', start + 1)
    row = html[start : nxt if nxt != -1 else len(html)]
    cell = row.index('data-label="Files"')
    return row[cell : row.index("</div>", cell)]


async def test_the_files_cell_says_what_a_pull_received_and_what_it_pruned(app, client):
    """A pull has no denominator and never will — `_estimate` prices the local index,
    which cannot know what the far end holds — so the cell used to print "—" over a job
    that had received two files. Saying nothing about the one number that *is* known is
    not the same restraint as declining to invent the one that is not.

    The prune is appended to either form and is deliberately not conditioned on the kind:
    a mirror Replicate deletes too, and that has never shown anywhere.
    """
    from libnodes.jobs import Job

    store = app.state.lib.jobs.store
    push = store.create(Job(id=0, device_id="kobo", sources=["Fiction"], label="Fiction",
                            state="done", files_total=5))
    push.files_sent = 5
    store.save(push)

    pull = store.create(Job(id=0, device_id="kobo", sources=["/Books/"], label="(pull)",
                            state="done", kind="pull"))
    pull.files_sent = 2
    pull.files_deleted = 1
    store.save(pull)

    html = (await client.get("/jobs")).text

    assert "5/5" in _files_cell(html, push.id)

    cell = _files_cell(html, pull.id)
    assert "—" not in cell, "the dash was the bug: two files were received"
    assert "2" in cell and "−1" in cell
    assert "2 files received, 1 deleted" in cell, "the title spells out both halves"
