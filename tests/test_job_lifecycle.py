"""Cancelling, dismissing, and not creating jobs the user did not ask for.

Every test here corresponds to something that actually went wrong in use.
"""

from __future__ import annotations

import asyncio

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
