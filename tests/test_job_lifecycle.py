"""Cancelling, dismissing, and not creating jobs the user did not ask for.

Every test here corresponds to something that actually went wrong in use.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

import pytest


async def test_offline_push_asks_before_creating_anything(client, app):
    """The bug: the job was created first and the dialog shown afterwards.

    Cancelling then left an un-asked-for job in the history with no way to remove it.
    """
    r = await client.post(
        "/jobs", data={"device": "kobo", "path": "Science/Physics/Landau.pdf"}
    )
    assert r.status_code == 200
    assert "is unreachable" in r.text
    # Nothing queued yet — the dialog is a question, not a receipt.
    assert app.state.lib.jobs.recent() == []


async def test_cancelling_the_dialog_leaves_no_job(client, app):
    """Cancel is client-side precisely because there is nothing to undo."""
    await client.post("/jobs", data={"device": "kobo", "path": "Science/Physics"})
    assert app.state.lib.jobs.recent() == []


async def test_confirming_the_dialog_queues_it_deferred(client, app):
    r = await client.post(
        "/jobs",
        data={
            "device": "kobo",
            "path": "Science/Physics/Landau.pdf",
            "confirmed": "yes",
        },
    )
    assert r.status_code == 200
    jobs = app.state.lib.jobs.recent()
    assert len(jobs) == 1
    assert jobs[0].state == "deferred"


async def test_dismiss_removes_a_deferred_card(client, app):
    """The bug: dismiss required job.finished, so a deferred card was undismissable."""
    lib = app.state.lib
    await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    job = lib.jobs.recent()[0]
    assert job.state == "deferred"
    assert any(j.id == job.id for j in lib.jobs.active())

    r = await client.post(f"/jobs/{job.id}/dismiss")
    assert r.status_code == 200
    assert not any(j.id == job.id for j in lib.jobs.active())
    assert not any(j.id == job.id for j in lib.jobs.settled())


async def test_delete_removes_the_job_from_history(client, app):
    lib = app.state.lib
    await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    job = lib.jobs.recent()[0]

    r = await client.request("DELETE", f"/jobs/{job.id}")
    assert r.status_code == 200
    assert lib.jobs.recent() == []
    assert lib.store.get(job.id) is None


async def test_delete_returns_the_refreshed_table(client, app):
    await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    job_id = app.state.lib.jobs.recent()[0].id
    r = await client.request("DELETE", f"/jobs/{job_id}")
    # The row is gone and the caller gets a body it can swap, not an empty 200.
    assert f"job-row-{job_id}" not in r.text
    assert "No jobs yet" in r.text


async def test_deleting_a_nonexistent_job_is_harmless(client):
    r = await client.request("DELETE", "/jobs/424242")
    assert r.status_code == 200


async def test_clear_finished_is_not_swallowed_by_the_job_id_route(client, app):
    """`/jobs/finished` and `/jobs/{job_id}` share a shape, and FastAPI matches in
    declaration order — so the literal has to be declared first or every Clear finished
    is a 422 on int("finished"), with the button appearing to do nothing."""
    lib = app.state.lib
    await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    done = lib.jobs.recent()[0]
    done.state = "done"
    done.finished_at = time.time()
    lib.store.save(done)

    r = await client.request("DELETE", "/jobs/finished")
    assert r.status_code == 200
    assert lib.jobs.recent() == []
    assert lib.store.get(done.id) is None


async def test_every_job_row_offers_a_way_out(client, app):
    """A job the user cannot remove is a bug regardless of its state."""
    await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    r = await client.get("/jobs/rows")
    job_id = app.state.lib.jobs.recent()[0].id
    assert f'hx-delete="/jobs/{job_id}"' in r.text


async def test_dismiss_repaints_the_dock(client, app):
    """hx-swap=none meant the card stayed on screen even when dismissal worked."""
    await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    job_id = app.state.lib.jobs.recent()[0].id
    r = await client.post(f"/jobs/{job_id}/dismiss")
    assert r.status_code == 200
    assert f'id="job-{job_id}"' not in r.text


async def test_push_to_a_reachable_device_skips_the_dialog(client, app, monkeypatch):
    from libnodes.probe import Reachability
    import time as _time

    lib = app.state.lib
    lib.probe._slot("kobo").reach = Reachability(
        state="online", last_ok=_time.time(), checked_at=_time.time()
    )
    r = await client.post(
        "/jobs", data={"device": "kobo", "path": "Science/Physics/Landau.pdf"}
    )
    assert "is unreachable" not in r.text
    assert lib.jobs.recent()[0].state in ("queued", "running", "done", "failed")


# ------------------------------------------------ multi-device pushes --


async def test_picker_offers_every_device(client):
    """Push is now 'choose devices', not 'ship to whatever is pinned elsewhere'."""
    r = await client.get("/jobs/picker", params={"path": "Science/Physics"})
    assert r.status_code == 200
    assert "Test Kobo" in r.text
    assert "Test Phone" in r.text
    assert 'name="device"' in r.text
    assert 'name="path" value="Science/Physics"' in r.text


async def test_picker_carries_the_whole_selection(client):
    r = await client.get(
        "/jobs/picker",
        params={"path": ["Science/Physics/Landau.pdf", "Science/Physics/Feynman.djvu"]},
    )
    assert r.text.count('name="path"') == 2
    assert "2 items" in r.text


async def test_picker_refuses_an_empty_selection(client):
    r = await client.get("/jobs/picker", params={"path": []})
    assert "nothing selected" in r.text


async def test_pushing_to_two_devices_creates_two_jobs(client, app):
    r = await client.post(
        "/jobs",
        data={
            "device": ["kobo", "phone"],
            "path": "Science/Physics",
            "confirmed": "yes",
        },
    )
    assert r.status_code == 200
    jobs = app.state.lib.jobs.recent()
    assert len(jobs) == 2
    assert {j.device_id for j in jobs} == {"kobo", "phone"}
    # One toast each, so the outcome is legible.
    assert r.text.count('class="toast"') == 2


async def test_one_unreachable_device_asks_before_creating_any(client, app):
    """The dialog is a question about the batch, not a receipt for part of it."""
    r = await client.post(
        "/jobs", data={"device": ["kobo", "phone"], "path": "Science/Physics"}
    )
    assert "is unreachable" in r.text
    assert app.state.lib.jobs.recent() == [], "queued something before asking"


async def test_no_pinned_target_remains(client):
    """PUSH TARGET is gone; nothing should still depend on a cookie-held device."""
    page = await client.get("/library")
    assert 'class="section-label">Push target' not in page.text
    assert "pinned target" not in page.text
    assert "libnodes_target" not in page.text
    assert "/lib/target" not in page.text
    # ...and the selection bar names no particular device any more.
    bar = await client.get("/lib/selection", params={"path": ["Science/Physics"]})
    assert "Push →" in bar.text
    assert "Push to " not in bar.text


async def test_picker_pre_checks_nothing(client):
    """A dialog that arrives with a device already ticked can push somewhere unintended
    — and the first device in devices.yaml may well be an offline one."""
    r = await client.get("/jobs/picker", params={"path": "Science/Physics"})
    assert "checked" not in r.text


async def test_library_row_offers_a_dry_run(client):
    """PRESENT ON answers "what is on the device", never "what would this push move".

    A push that repairs a handful of files already present is indistinguishable from one
    that re-sends the directory unless the dry run is one click from the row.
    """
    r = await client.get("/lib/list", params={"p": "Science"})
    assert "/jobs/picker?dry_run=true&amp;path=" in r.text


def test_a_row_push_survives_a_quote_in_the_book_name(app):
    """`hx-vals` is JSON inside a single-quoted attribute, so `|e` is the wrong escape:
    it writes `&#34;`, the parser hands htmx back a bare `"`, and the object no longer
    parses — that row's push buttons go dead. Rendered here rather than through the
    fixture library, whose file counts several other tests assert on.
    """
    from libnodes.library import Entry
    from libnodes.templating import templates

    row = Entry(
        path='Fiction/He said "hi".epub',
        parent="Fiction",
        name='He said "hi".epub',
        is_dir=False,
        fmt="epub",
        size=1024,
        mtime=0,
        files=None,
        blob=None,
        title=None,
        author=None,
    )
    device = app.state.lib.devices.config.devices[0]
    html = templates.env.get_template("file_rows.html").render(
        rows=[row],
        presence={},
        by_id={},
        devices=[device],
        push_devices=[device],
        q="",
        path="",
        oob=False,
    )

    vals = re.findall(r"hx-vals='([^']*)'", html)
    assert vals, "the row rendered no push button at all"
    assert json.loads(vals[0])["path"] == 'Fiction/He said "hi".epub'


async def test_the_selection_bar_asks_the_picker_for_a_dry_run(client):
    """The bulk button is the other way in, and it used to send `?from=selection`.

    Nothing reads `from`, so the picker rendered as an ordinary push dialog: a button
    labelled "Dry run…" that opened "Push to…" and would have queued a real transfer.
    """
    r = await client.get("/lib/selection", params={"path": ["Science/Physics"]})
    assert "/jobs/picker?dry_run=true" in r.text
    assert "from=selection" not in r.text


async def test_dry_run_picker_posts_to_the_dry_run_endpoint(client):
    r = await client.get(
        "/jobs/picker", params={"path": "Science/Physics", "dry_run": "true"}
    )
    assert 'hx-post="/jobs/dry-run"' in r.text


async def test_the_picker_closes_only_after_its_request(client):
    """See test_no_button_removes_itself_while_asking_for_something in test_routes.py —
    closing in `onclick` detaches the button and htmx abandons the push in silence."""
    r = await client.get("/jobs/picker", params={"path": "Science/Physics"})
    assert "hx-on::after-request" in r.text


async def test_the_offline_dialog_closes_only_after_its_request(client):
    """Same defect, one step further along: both fixture devices are unreachable, so a
    push lands here, and its Queue push button was dead for the same reason."""
    r = await client.post(
        "/jobs", data={"device": "kobo", "path": "Science/Physics"}
    )
    assert "is unreachable" in r.text
    assert "hx-on::after-request" in r.text


async def test_dry_run_accepts_several_devices(client, app):
    r = await client.post(
        "/jobs/dry-run",
        data={"device": ["kobo", "phone"], "path": "Science/Physics"},
    )
    assert r.status_code == 200
    jobs = app.state.lib.jobs.recent()
    assert len(jobs) == 2
    assert all(j.dry_run for j in jobs)
    assert all("-n" in j.argv for j in jobs)


def _source_cell(html: str) -> str:
    """The SOURCE column's text for the first job row, badges and markup stripped."""
    cell = re.search(r'data-label="Source"[^>]*>(.*?)</div>', html, re.S)
    assert cell, "no Source cell in the jobs table"
    return " ".join(re.sub(r"<[^>]*>", " ", cell.group(1)).split())


async def test_a_whole_library_push_is_named_for_the_library(client, settings):
    """Selecting every top-level directory is the library, and the SOURCE column should
    say so. It used to read `/Books/Art +16 (full)`: `Art` only because it sorts first,
    and `(full)` because `len(sources) > 3` — which called any four directories the whole
    library, and still named one of them at random when it really was."""
    from libnodes.jobs import full_sync_sources

    everything = full_sync_sources(settings)
    assert len(everything) > 1, "fixture has too few top-level directories to be a test"

    await client.post(
        "/jobs", data={"device": "kobo", "path": everything, "confirmed": "yes"}
    )
    rows = await client.get("/jobs/rows")
    assert _source_cell(rows.text) == str(settings.library_root)


async def test_a_partial_push_still_names_what_it_sends(client, settings):
    """The counterpart: naming the library for anything less would be a lie."""
    await client.post(
        "/jobs",
        data={
            "device": "kobo",
            "path": ["Science/Physics", "Fiction"],
            "confirmed": "yes",
        },
    )
    rows = await client.get("/jobs/rows")
    cell = _source_cell(rows.text)
    assert cell.startswith(f"{settings.library_root}/")
    assert cell.endswith("+1")
    assert str(settings.library_root) != cell


async def test_a_single_directory_push_names_it_plainly(client, settings):
    await client.post(
        "/jobs", data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"}
    )
    rows = await client.get("/jobs/rows")
    assert _source_cell(rows.text) == f"{settings.library_root}/Science/Physics"


async def test_a_full_sync_source_cell_is_a_path_not_a_caption(client, settings):
    """Full Sync sets label="(full library)", which the cell used to print as
    `/Books/(full library)` — a caption pasted where a path belongs."""
    await client.post("/device/kobo/full-sync")
    rows = await client.get("/jobs/rows")
    assert _source_cell(rows.text) == str(settings.library_root)


async def test_a_queued_toast_names_the_device_not_its_yaml_id(client):
    """queued.html looks the device up in `by_id`, which base_context did not supply, so
    every push and dry run announced itself with the raw id from devices.yaml."""
    r = await client.post(
        "/jobs/dry-run", data={"device": "kobo", "path": "Science/Physics"}
    )
    assert "Test Kobo" in r.text


async def test_dry_run_never_asks_about_reachability(client, app):
    """Both fixture devices are unreachable; a dry run should still just run."""
    r = await client.post(
        "/jobs/dry-run", data={"device": "kobo", "path": "Science/Physics"}
    )
    assert "is unreachable" not in r.text
    assert app.state.lib.jobs.recent()[0].dry_run is True


# --------------------------------------------------- logs live with jobs --


async def test_there_is_no_separate_logs_section(client):
    """A log belongs to its job. A global list of them was a second place to look."""
    assert (await client.get("/logs")).status_code == 404
    page = await client.get("/jobs")
    assert 'href="/logs"' not in page.text


async def test_every_job_that_ran_offers_its_log(client, app, tmp_path, monkeypatch):
    fake = tmp_path / "r"
    fake.write_text("#!/bin/sh\necho 'sending incremental file list'\nexit 0\n")
    fake.chmod(0o755)
    async with app.router.lifespan_context(app):
        monkeypatch.setattr("libnodes.jobs.build_argv", lambda *a, **k: [str(fake)])
        lib = app.state.lib
        job = lib.jobs.submit(lib.devices.config.by_id["kobo"], ["Fiction"])
        for _ in range(80):
            if lib.jobs.get(job.id).finished:
                break
            await asyncio.sleep(0.05)

        rows = await client.get("/jobs/rows")
        assert f'hx-get="/jobs/{job.id}/log/view"' in rows.text

        view = await client.get(f"/jobs/{job.id}/log/view")
        assert view.status_code == 200
        assert "sending incremental file list" in view.text
        assert f"Job #{job.id}" in view.text


async def test_a_job_that_never_ran_offers_no_log(client, app):
    """A deferred job has nothing to show, and should not pretend otherwise."""
    await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    job = app.state.lib.jobs.recent()[0]
    assert job.started_at is None

    rows = await client.get("/jobs/rows")
    assert f"/jobs/{job.id}/log/view" not in rows.text

    view = await client.get(f"/jobs/{job.id}/log/view")
    assert "No log for this job" in view.text


async def test_log_view_tails_a_large_log(client, app, settings):
    """A full-library run writes ~400 KB; the dialog shows the end and links the rest."""
    from libnodes.routes.jobs import LOG_TAIL_LINES

    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    (settings.logs_dir / "4242.log").write_text(
        "\n".join(f"line {i}" for i in range(LOG_TAIL_LINES + 500))
    )
    r = await client.get("/jobs/4242/log/view")
    assert "line 0" not in r.text           # the head is dropped
    assert f"line {LOG_TAIL_LINES + 499}" in r.text   # the tail is kept
    assert "earlier lines omitted" in r.text
    assert 'href="/jobs/4242/log"' in r.text          # ...and the full file is offered


async def test_an_idle_jobs_page_polls_slowly(client, app):
    """An idle page left open was asking the Pi for telemetry every 3s for ever.
    Nothing changes between those polls when nothing is running."""
    idle = await client.get("/jobs")
    assert 'hx-trigger="every 20s"' in idle.text
    assert 'hx-trigger="every 3s"' not in idle.text


async def test_a_running_job_makes_the_page_poll_fast(client, app, tmp_path, monkeypatch):
    slow = tmp_path / "r"
    slow.write_text("#!/bin/sh\nsleep 3\nexit 0\n")
    slow.chmod(0o755)
    async with app.router.lifespan_context(app):
        monkeypatch.setattr("libnodes.jobs.build_argv", lambda *a, **k: [str(slow)])
        lib = app.state.lib
        lib.jobs.submit(lib.devices.config.by_id["kobo"], ["Fiction"])
        for _ in range(40):
            await asyncio.sleep(0.05)
            if lib.jobs.counts()[0]:
                break
        page = (await client.get("/jobs")).text
        assert 'hx-trigger="every 3s"' in page


async def test_the_log_dialog_offers_one_way_to_the_raw_file(client, app, settings):
    """Two controls doing the same thing is one too many."""
    from libnodes.routes.jobs import LOG_TAIL_LINES

    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    (settings.logs_dir / "77.log").write_text(
        "\n".join(f"line {i}" for i in range(LOG_TAIL_LINES + 10))
    )
    r = await client.get("/jobs/77/log/view")
    assert r.text.count('href="/jobs/77/log"') == 1
