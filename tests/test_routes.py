"""Route behaviour, and the HTMX contract every fragment has to honour."""

from __future__ import annotations

import re

import pytest

from libnodes.templating import TEMPLATES_DIR

FRAGMENTS = [
    "/devices/rows",
    "/devices/grid",
    "/devices/status",
    "/lib/pane",
    "/lib/list",
    "/lib/tree",
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


# ------------------------------------------------------- library tree --

async def test_tree_rows_carry_toggle_state(client):
    """The disclosure needs depth and path to toggle a flat list client-side."""
    r = await client.get("/lib/tree", params={"p": "Science/Physics"})
    assert "data-tree-toggle" in r.text
    assert 'data-depth="0"' in r.text
    assert 'data-path="Science"' in r.text
    # Ancestors of the selection open by default; siblings do not.
    assert 'data-collapsed="0"' in r.text
    assert 'data-collapsed="1"' in r.text


async def test_tree_caret_is_separate_from_the_navigation_link(client):
    """Clicking the triangle must not navigate — they are two controls, not one."""
    r = await client.get("/lib/tree")
    assert "<button class=\"tree-caret\"" in r.text
    assert 'class="tree-link"' in r.text


async def test_tree_children_returns_one_level(client):
    r = await client.get("/lib/tree/children", params={"p": "Science", "depth": 0})
    assert r.status_code == 200
    assert 'data-path="Science/Physics"' in r.text
    assert 'data-depth="1"' in r.text
    # One level only: grandchildren are not included.
    assert "Science/Physics/" not in r.text
    # A bare fragment, swappable straight into the flat list.
    assert "<aside" not in r.text


async def test_tree_children_rejects_paths_outside_the_index(client):
    assert (await client.get("/lib/tree/children", params={"p": "../etc"})).status_code == 400
    assert (await client.get("/lib/tree/children", params={"p": ".data"})).status_code == 400
