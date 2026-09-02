"""Route behaviour, and the HTMX contract every fragment has to honour."""

from __future__ import annotations

import re

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
    "/devices.yaml/panel",
]

PAGES = ["/devices", "/library", "/jobs", "/devices.yaml", "/presets", "/keys"]


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


async def test_a_grid_page_keeps_its_cards_when_filtered_or_rescanned(client):
    client.cookies.set("libnodes_view", "grid")

    page = await client.get("/devices")
    assert 'hx-get="/devices/grid"' in page.text, "the filter box still names the table"

    rescan = await client.post("/devices/rescan")
    assert 'class="cards"' in rescan.text
    assert 'class="trow' not in rescan.text
    # ...and the follow-up that collects the sweep has to come back as cards too.
    assert 'hx-get="/devices/grid"' in rescan.text


async def test_a_retry_in_grid_replaces_one_card(client):
    client.cookies.set("libnodes_view", "grid")
    r = await client.post("/device/kobo/probe")
    assert 'id="card-kobo"' in r.text
    assert 'id="node-kobo"' not in r.text


async def test_a_retry_in_table_still_replaces_one_row(client):
    r = await client.post("/device/kobo/probe")
    assert 'id="node-kobo"' in r.text
    assert 'id="card-kobo"' not in r.text


async def test_switching_view_keeps_the_filter(client):
    r = await client.get("/devices", params={"q": "phone", "view": "table"})
    assert "/devices?view=grid&amp;q=phone" in r.text


async def test_library_lists_real_entries(client):
    r = await client.get("/lib/list", params={"p": "Science/Physics"})
    assert "Feynman.djvu" in r.text
    assert "Landau.pdf" in r.text


async def test_library_filter_reports_counts(client):
    r = await client.get("/lib/list", params={"p": "", "q": "Feynman"})
    assert "Feynman.djvu" in r.text
    assert "matches" in r.text


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


async def test_devices_yaml_view_highlights_and_counts(client):
    r = await client.get("/devices.yaml")
    assert "Test Kobo" in r.text
    assert 'class="y-key"' in r.text
    assert "2 devices" in r.text


async def test_devices_yaml_validation_strip_reports_bad_edits(client, devices_file):
    devices_file.write_text(
        devices_file.read_text().replace("port: 2222", 'port: "2222 "')
    )
    r = await client.post("/devices.yaml/validate")
    assert "devices[0].port" in r.text
    assert "line " in r.text


async def test_reload_from_disk_returns_the_actual_panel(client):
    """The regression: Reload used to swap in only the validation strip.

    For a *valid* file that strip is empty, so the button visibly did nothing at all.
    """
    r = await client.get("/devices.yaml/panel")
    assert r.status_code == 200
    assert "Test Kobo" in r.text          # the code is really there
    assert 'class="y-key"' in r.text      # highlighted
    assert "2 devices" in r.text            # header refreshed too
    assert "Copy" in r.text               # and the control survives the swap
    assert len(r.text) > 500


async def test_reload_from_disk_picks_up_an_external_edit(client, devices_file):
    """The whole point of the button: see what is on disk right now."""
    before = await client.get("/devices.yaml/panel")
    assert "Renamed Kobo" not in before.text

    devices_file.write_text(
        devices_file.read_text().replace("name: Test Kobo", "name: Renamed Kobo")
    )

    after = await client.get("/devices.yaml/panel")
    assert "Renamed Kobo" in after.text


async def test_config_edits_apply_without_any_button(client, devices_file):
    """The mtime watcher is the real mechanism — no reload action is involved.

    This is why the panel's button is a view refresh, not a config reload.
    """
    assert "Renamed Kobo" not in (await client.get("/devices/rows")).text
    devices_file.write_text(
        devices_file.read_text().replace("name: Test Kobo", "name: Renamed Kobo")
    )
    assert "Renamed Kobo" in (await client.get("/devices/rows")).text


async def test_validation_strip_alone_is_empty_when_valid(client):
    """Documents why the bug existed — this fragment is legitimately blank."""
    r = await client.post("/devices.yaml/validate")
    assert r.status_code == 200
    assert r.text.strip() == ""


async def test_panel_shows_the_error_band_for_a_bad_edit(client, devices_file):
    devices_file.write_text(
        devices_file.read_text().replace("port: 2222", 'port: "2222 "')
    )
    r = await client.get("/devices.yaml/panel")
    assert "devices[0].port" in r.text
    assert "is-bad" in r.text  # the offending line is highlighted in the gutter


async def test_devices_yaml_download(client):
    r = await client.get("/devices.yaml/raw")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert "devices:" in r.text


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
    """It drops q/fmt/sort, and that is not a judgement call: `children()` appends
    `is_dir = 0` for a query and tests `fmt IN (...)`, which a directory's NULL fmt can
    never match — so a directory row only exists when both are empty."""
    for params in ({"q": "feyn"}, {"fmt": ["epub"]}):
        r = await client.get("/lib/pane", params=params)
        rows = r.text.split('<div id="file-rows">')[1]
        assert '<a class="file-name"' not in rows, f"a directory row survived {params}"


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


async def test_the_only_reindex_control_survived_the_tree(client):
    """It lived in the tree pane's header alone. Deleting that pane without moving it
    would have left the route and the status chip working and no way to reach either."""
    r = await client.get("/lib/pane")
    assert 'hx-post="/lib/reindex"' in r.text
    # In the filter bar, not orphaned somewhere below the table.
    assert r.text.index('hx-post="/lib/reindex"') < r.text.index('class="lib-body"')


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
