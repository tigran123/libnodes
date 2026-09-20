"""`prune: true` — the third place in the program that emits `--delete`.

A Full Sync has always promised adds and updates only, and a device therefore kept every
book the library ever retired. Measured against s4l on 2026-09-20 with exactly the flags
`build_argv` composes: one book removed from the library months earlier was still sitting
there, and a dry run reported "0 files" with nothing to say about it.

What makes this affordable is the same thing that makes the mirror's `--delete`
affordable, reached from a different side. rsync prunes only inside the directories it is
transferring, and a reader's sources are the *named* top-level categories — so a name the
device holds and the library has never had is never scanned, and only divergence inside
the library's own shape is pruned. `excludes` cover the rest: rsync never deletes what one
matched, which is what keeps KOReader's `.sdr` sidecars — reading position, bookmarks,
highlights, written *inside* the library tree — alive through a prune. On s4l that is the
difference between 20 deletions and 1.

Every assertion here is paired with its does-not-prune counterpart, because the point is
not that a node can delete: it is that four separate facts have to line up before one can,
and each of them rules out a different way of arriving with the wrong scope.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from libnodes.jobs import build_argv, full_sync_sources


@pytest.fixture
def devices_file(settings) -> Path:
    """Overrides the shared fixture: this module needs a node that asked to be pruned.

    `kobo` asks, `phone` does not, and the two are otherwise the same kind of reader —
    which is the comparison most of these tests are.
    """
    path = settings.resolved_devices_file
    path.write_text(
        """
defaults:
  timeout: 20
  retries: 0
  # Fleet-wide, because every KOReader node writes these and each one of them is inside
  # the library tree. An exclude is a protection as much as a filter.
  excludes: ["*.sdr/"]

devices:
  - id: kobo
    name: Test Kobo
    abbr: TK
    type: kobo
    host: 127.0.0.1
    port: 2222
    user: root
    target: /mnt/onboard/Books
    full_sync: true
    prune: true
    capacity: 29G

  - id: phone
    name: Test Phone
    abbr: PH
    type: termux
    host: 127.0.0.1
    port: 8022
    user: u0_a1
    target: /sdcard/Books
    full_sync: true

  - id: rooted
    name: Test Rooted
    abbr: RT
    type: linux
    fs: ext4
    host: 127.0.0.1
    port: 22
    user: root
    target: /
    full_sync: true
    prune: true

  - id: thinkpad
    name: Test ThinkPad
    abbr: TP
    type: linux
    fs: ext4
    host: 127.0.0.1
    port: 22
    user: root
    target: /srv/books
    sync_mode: mirror
    prune: true
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _argv(app, device_id, **kw):
    lib = app.state.lib
    device = lib.devices.config.by_id[device_id]
    return build_argv(
        device,
        lib.devices.config,
        full_sync_sources(lib.settings),
        lib.settings,
        **kw,
    )


# ------------------------------------------------------------------ the flag --


def test_a_full_sync_prunes_where_the_node_asked_for_it(app):
    argv = _argv(app, "kobo", whole_library=True)
    assert "--delete" in argv
    # The sources stay the named categories. `./` is the mirror's answer and it is the
    # wrong one here: it would put the destination *root* in the transfer, so anything
    # the device keeps beside the library would be pruned too.
    assert "./" not in argv
    assert any(a == "Fiction/" for a in argv)


def test_a_node_that_did_not_ask_keeps_the_old_promise(app):
    """The default, and it is the promise Full Sync's own note has always made."""
    assert "--delete" not in _argv(app, "phone", whole_library=True)


def test_a_subtree_push_never_prunes(app):
    """`--delete` prunes the directories in the transfer.

    So a Push of `Science/` would mean "and remove everything under Science/ that is not
    in the library" — under a button that says Push and shows no such promise. The scope
    of a prune has to be the scope the user was shown, and only Full Sync shows it.
    """
    lib = app.state.lib
    argv = build_argv(
        lib.devices.config.by_id["kobo"],
        lib.devices.config,
        ["Science/Physics"],
        lib.settings,
    )
    assert "--delete" not in argv


def test_an_adopt_never_prunes(app):
    """`--size-only` paired with "delete whatever does not match" is a trap.

    The same reasoning that keeps a mirror's Adopt clean: that run exists to repair
    timestamps on files already in place, and it compares by size alone.
    """
    assert "--delete" not in _argv(app, "kobo", whole_library=True, adopt=True)


def test_the_dry_run_carries_the_prune_it_is_previewing(app):
    """A dry run is the only preview of a prune, so it must not preview a different one.

    `-n` is what makes that safe. The mirror entry in CLAUDE.md says the same thing about
    Replicate; this is that rule reaching a reader.
    """
    argv = _argv(app, "kobo", whole_library=True, dry_run=True)
    assert "-n" in argv and "--delete" in argv


def test_an_exclude_is_what_survives_a_prune(app):
    """rsync does not delete what an `--exclude` matched — unless told to.

    `--delete-excluded` would invert exactly that, and it is the one flag that would turn
    this feature into the thing it was designed around: KOReader's `<book>.sdr` sidecars
    live *inside* the library tree, so without the exclude a prune takes every reading
    position, bookmark and highlight on the device with it. Measured on s4l: 20 deletions
    without it, 1 with.
    """
    argv = _argv(app, "kobo", whole_library=True)
    assert "--exclude=*.sdr/" in argv
    assert "--delete-excluded" not in argv


# -------------------------------------------------------------- the refusals --


def test_a_prune_refuses_an_empty_source_list(app):
    """`full_sync_sources` returns [] when it cannot read the library root at all.

    Queueing that with `--delete` on is not a wrong transfer, it is an emptied device.
    The same refusal a mirror gets, for the same reason.
    """
    lib = app.state.lib
    with pytest.raises(ValueError, match="no sources"):
        build_argv(
            lib.devices.config.by_id["kobo"], lib.devices.config, [], lib.settings,
            whole_library=True,
        )


def test_a_prune_refuses_a_target_at_the_root(app):
    with pytest.raises(ValueError, match="below the root"):
        _argv(app, "rooted", whole_library=True)


def test_a_target_at_the_root_is_still_fine_without_the_prune(app):
    """The refusal is about `--delete`, not about the target, and says so by scope."""
    lib = app.state.lib
    argv = build_argv(
        lib.devices.config.by_id["rooted"],
        lib.devices.config,
        ["Science/Physics"],
        lib.settings,
    )
    assert "--delete" not in argv


def test_the_key_means_nothing_off_a_books_node(app):
    """A mirror already deletes — that is what the mode *is*.

    Reading a second key as though it governed that would be a way to talk someone into
    believing `prune: false` made a Replicate safe. Coerced off in the model, so nothing
    downstream has to remember.
    """
    device = app.state.lib.devices.config.by_id["thinkpad"]
    assert device.prune is False
    # And its own --delete is untouched by that coercion.
    assert "--delete" in _argv(app, "thinkpad")


# ------------------------------------------------------------------- the route --


async def test_the_full_sync_route_queues_the_prune(client, app):
    r = await client.post("/device/kobo/full-sync")
    assert r.status_code == 200
    job = app.state.lib.jobs.recent()[0]
    assert "--delete" in job.argv
    assert job.full_library is True
    assert app.state.lib.jobs.store.get(job.id).full_library is True, (
        "persisted: retry replays a stored row long after the route that set it ran"
    )


async def test_a_selection_push_to_the_same_node_does_not(client, app):
    r = await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    assert r.status_code == 200
    job = app.state.lib.jobs.recent()[0]
    assert "--delete" not in job.argv
    assert job.full_library is False


async def test_a_retried_full_sync_still_prunes(client, app):
    """Re-derived rather than replayed, so "full library" keeps meaning what it says."""
    await client.post("/device/kobo/full-sync")
    first = app.state.lib.jobs.recent()[0]

    r = await client.post(f"/jobs/{first.id}/retry")
    assert r.status_code == 200
    again = app.state.lib.jobs.recent()[0]
    assert again.id != first.id
    assert again.full_library is True
    assert "--delete" in again.argv
    assert again.label == "(full library)"
    assert set(again.sources) == set(full_sync_sources(app.state.lib.settings))


async def test_a_retried_dry_run_is_still_a_dry_run(client, app):
    """The one place a retry could turn a preview into an irreversible prune."""
    await client.post("/device/kobo/dry-run")
    preview = app.state.lib.jobs.recent()[0]
    assert preview.dry_run is True and "--delete" in preview.argv

    await client.post(f"/jobs/{preview.id}/retry")
    again = app.state.lib.jobs.recent()[0]
    assert again.dry_run is True
    assert "-n" in again.argv


# ---------------------------------------------------------------------- the UI --


async def test_the_menu_stops_promising_it_never_deletes(client):
    """The note is the only place the difference is stated in words.

    The command above it already shows the `--delete`; a note still reading "never
    deletes anything" beside it would be the more believable of the two.
    """
    menu = await client.get("/device/kobo/menu")
    assert "--delete" in menu.text
    assert "never deletes anything" not in menu.text
    assert "DELETES" in menu.text, "the confirm has to say so too"


async def test_the_menu_still_promises_it_for_a_node_that_did_not_ask(client):
    menu = await client.get("/device/phone/menu")
    assert "never deletes anything" in menu.text
    assert "--delete" not in menu.text


# ------------------------------------------------------------- the manifest --


def _rsync_that_deletes(tmp_path: Path) -> Path:
    script = tmp_path / "fake-rsync-prune"
    script.write_text(
        "#!/bin/sh\n"
        "echo '@480|Fiction/Aldiss/White-Mars.epub'\n"
        "echo 'deleting Science/Klassiki-Nauki/Clifford/Common-Sense-1946.pdf'\n"
        "echo 'deleting Science/Klassiki-Nauki/Clifford/'\n"
        "echo 'sent 2,060 bytes  received 57 bytes  4,234.00 bytes/sec'\n"
        "exit 0\n"
    )
    script.chmod(0o755)
    return script


async def test_a_push_retracts_the_rows_it_pruned(app, tmp_path, monkeypatch):
    """The same debit a pull takes, pointed at the other end.

    Without it the manifest goes on claiming the device holds a book the push has just
    removed — and that is not merely untidy: `presence` counts a directory's files as a
    range over `(device_id, path)`, so the stale row adds to the numerator of a fraction
    whose denominator has just lost one. A mirror Replicate had this gap too.

    The directory line is in the fixture on purpose: rsync removes a directory once its
    contents have gone and announces it the same way, and counting it under a heading
    that says FILES is the `to-chk` mistake again.
    """
    lib = app.state.lib
    lib.manifests.record(
        "kobo",
        [
            ("Science/Klassiki-Nauki/Clifford/Common-Sense-1946.pdf", "cd61", 9, 1, 0),
            ("Science/Physics/Feynman.djvu", "abc", 12, 1, 0),
        ],
        source="push",
    )
    fake = _rsync_that_deletes(tmp_path)
    monkeypatch.setattr("libnodes.jobs.build_argv", lambda *a, **k: [str(fake)])
    async with app.router.lifespan_context(app):
        job = lib.jobs.submit(lib.devices.config.by_id["kobo"], ["Fiction"])
        for _ in range(120):
            if lib.jobs.get(job.id).finished:
                break
            await asyncio.sleep(0.05)
        job = lib.jobs.get(job.id)
        held = lib.manifests.paths_for("kobo")

    assert job.state == "done"
    assert job.files_deleted == 1, "one file and one directory; a directory is not a file"
    assert lib.jobs.store.get(job.id).files_deleted == 1
    assert "Science/Klassiki-Nauki/Clifford/Common-Sense-1946.pdf" not in held
    assert "Science/Physics/Feynman.djvu" in held


async def test_a_dry_run_retracts_nothing(app, tmp_path, monkeypatch):
    """It deleted nothing, so the rows it "saw deleted" are still true."""
    lib = app.state.lib
    lib.manifests.record(
        "kobo",
        [("Science/Klassiki-Nauki/Clifford/Common-Sense-1946.pdf", "cd61", 9, 1, 0)],
        source="push",
    )
    fake = _rsync_that_deletes(tmp_path)
    monkeypatch.setattr("libnodes.jobs.build_argv", lambda *a, **k: [str(fake)])
    async with app.router.lifespan_context(app):
        job = lib.jobs.submit(
            lib.devices.config.by_id["kobo"], ["Fiction"], dry_run=True
        )
        for _ in range(120):
            if lib.jobs.get(job.id).finished:
                break
            await asyncio.sleep(0.05)
        held = lib.manifests.paths_for("kobo")

    assert "Science/Klassiki-Nauki/Clifford/Common-Sense-1946.pdf" in held
