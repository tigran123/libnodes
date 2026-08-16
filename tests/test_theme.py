"""Theme selection, and the held-job semantics of the offline dialog."""

from __future__ import annotations


# --------------------------------------------------------------------- theme --


def test_btn_default_does_not_wear_the_hover_background():
    """An emphasised button must not arrive already looking pressed.

    `.btn-default` used to set `background: var(--hover)` -- the exact background
    `.btn:hover` paints -- so it rendered permanently hovered and had no hover response
    left. Invisible while a whole group wore it (the Actions dialog), obvious the moment
    a plain `.btn` stood beside one (Test next to Actions on a device row).

    Asserted against the stylesheet because there is no browser here to read a computed
    style from; the rule is short enough that its text is the behaviour.
    """
    from pathlib import Path

    css = Path(__file__).resolve().parent.parent / "libnodes" / "static" / "app.css"
    block = css.read_text().split(".btn-default {")[1].split("}")[0]
    assert "background" not in block
    assert "border-color" in block          # still distinguishable from a plain .btn


async def test_defaults_to_dark(client):
    r = await client.get("/devices")
    assert 'data-theme="light"' not in r.text


async def test_light_cookie_is_rendered_server_side(client):
    """Server-stamped rather than JS-applied, so there is no flash of dark first."""
    r = await client.get("/devices", cookies={"libnodes_theme": "light"})
    assert 'data-theme="light"' in r.text


async def test_toggle_is_present_on_every_page(client):
    for path in ("/devices", "/library", "/jobs", "/devices.yaml", "/keys"):
        r = await client.get(path)
        assert "data-theme-toggle" in r.text, path


async def test_unknown_cookie_value_falls_back_to_dark(client):
    r = await client.get("/devices", cookies={"libnodes_theme": "banana"})
    assert 'data-theme="light"' not in r.text


# ---------------------------------------------------------------- held jobs --


async def test_unticking_auto_start_holds_the_job(client, app):
    """The bug: the checkbox was disabled, so this could not be expressed at all."""
    r = await client.post(
        "/jobs",
        data={
            "device": "kobo",
            "path": "Science/Physics",
            "confirmed": "yes",
            # "auto" absent = the user cleared the box
        },
    )
    assert r.status_code == 200
    job = app.state.lib.jobs.recent()[0]
    assert job.state == "deferred"
    assert job.hold is True


async def test_leaving_auto_start_ticked_does_not_hold(client, app):
    await client.post(
        "/jobs",
        data={
            "device": "kobo",
            "path": "Science/Physics",
            "confirmed": "yes",
            "auto": "on",
        },
    )
    job = app.state.lib.jobs.recent()[0]
    assert job.state == "deferred"
    assert job.hold is False


async def test_watcher_ignores_held_jobs(client, app, monkeypatch):
    """A held job must not start just because the device turned up."""
    import time

    from libnodes.probe import Reachability

    lib = app.state.lib
    await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    job = lib.jobs.recent()[0]
    assert job.hold is True

    # The node comes online; the watcher's promotion rule must still skip it.
    lib.probe._slot("kobo").reach = Reachability(
        state="online", last_ok=time.time(), checked_at=time.time()
    )
    promotable = [
        j
        for j in lib.jobs.active()
        if j.state == "deferred"
        and not j.hold
        and lib.probe.status(j.device_id).online
    ]
    assert promotable == []


async def test_held_job_can_be_started_manually(client, app):
    lib = app.state.lib
    await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    job = lib.jobs.recent()[0]

    r = await client.post(f"/jobs/{job.id}/start")
    assert r.status_code == 200
    after = lib.jobs.get(job.id)
    assert after.hold is False
    assert after.state in ("queued", "running", "failed", "done")


async def test_held_job_row_offers_start(client, app):
    await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    job_id = app.state.lib.jobs.recent()[0].id
    r = await client.get("/jobs/rows")
    assert "HELD" in r.text
    assert f'hx-post="/jobs/{job_id}/start"' in r.text


async def test_hold_survives_a_restart(settings, app, client):
    """hold is persisted, so a restart does not silently make a held job auto-run."""
    from libnodes.jobs import JobStore

    await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    job_id = app.state.lib.jobs.recent()[0].id

    fresh = JobStore(settings.jobs_db)
    assert fresh.get(job_id).hold is True


def test_schema_migration_adds_hold_to_an_old_database(tmp_path):
    """Existing deployments have a jobs.db without the column."""
    import sqlite3

    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,"
        " sources TEXT NOT NULL, label TEXT NOT NULL, dest TEXT, state TEXT NOT NULL,"
        " created_at REAL, started_at REAL, finished_at REAL, files_done INTEGER,"
        " files_total INTEGER, bytes_done INTEGER, bytes_total INTEGER, pct REAL,"
        " exit_code INTEGER, error TEXT, argv TEXT, attempt INTEGER, dry_run INTEGER);"
    )
    conn.execute(
        "INSERT INTO jobs (device_id, sources, label, state) VALUES ('d','[]','x','done')"
    )
    conn.commit()
    conn.close()

    from libnodes.jobs import JobStore

    store = JobStore(db)          # must migrate, not explode
    job = store.recent()[0]
    assert job.hold is False
    # files_sent/entries_* arrived the same way. The old files_done column is left where
    # it is: its rows hold entry counts, and relabelling those as transfers would invent
    # history rather than migrate it.
    assert job.files_sent == 0
    assert job.entries_total == 0
