"""`sync_mode: upstream` — the direction reversed, and everything that must refuse it.

A reader and a mirror are both *destinations*: LibNodes composes a command and points it
at them. An upstream is the opposite end of the same wire — the production host other
admins upload to, so it is ahead of us and a push would be a regression.

Every assertion here is paired with its push counterpart, because the hazard being removed
is not hypothetical. sigmaai.au was declared `mirror`, which put Replicate in its Actions
menu, and Replicate composes `rsync -a --delete ./ tigran@sigmaai.au:/Books/` — pointed at
the server everyone uploads to, it would have deleted every book production held and pi5
did not.

The fixture fleet below is deliberately hostile in three ways, and each one is a trap that
was live at some point while this was written:

* the upstream declares `full_sync: true`, because `full_sync` is mutually exclusive with
  *mirror* and with nothing else — so the moment a node stops being a mirror the `elif` in
  device_menu.html fires and Full Sync, a push, reappears;
* it declares `fs: vfat` and `stores_times: false`, so a pull that merely *happened* not to
  emit the device-as-destination flags is distinguishable from one that cannot;
* it keeps a mirror beside it, so nothing here can pass by widening `is_mirror`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from libnodes.config import PULL_EXCLUDES
from libnodes.jobs import (
    build_argv,
    build_catalog_argv,
    build_pull_argv,
    cleanup_argv,
    mirror_sources,
    service_argv,
    snapshot_argv,
)
from libnodes.scan import scan_argv


@pytest.fixture
def devices_file(settings) -> Path:
    """Overrides the shared fixture. See the module docstring for why each key is here."""
    path = settings.resolved_devices_file
    path.write_text(
        """
defaults:
  timeout: 20
  retries: 0

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

  - id: thinkpad
    name: Test ThinkPad
    abbr: TP
    type: linux
    fs: ext4
    host: 127.0.0.1
    port: 22
    user: tigran
    target: /Books
    sync_mode: mirror

  - id: source
    name: Test Upstream
    abbr: SRC
    type: linux
    host: 127.0.0.1
    port: 22
    user: tigran
    target: /Books
    fs: vfat
    stores_times: false
    full_sync: true
    sync_mode: upstream
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _device(app, device_id):
    return app.state.lib.devices.config.by_id[device_id]


def _pull(app, settings, **kw):
    lib = app.state.lib
    return build_pull_argv(_device(app, "source"), lib.devices.config, settings, **kw)


# ------------------------------------------------------------------ the flags --


def test_the_default_is_still_books_so_nothing_moved_under_the_fleet(app):
    assert _device(app, "kobo").sync_mode == "books"
    assert _device(app, "kobo").is_upstream is False
    assert _device(app, "thinkpad").is_upstream is False
    assert _device(app, "source").is_upstream is True


def test_a_pull_never_dereferences_the_vault(app, settings):
    """No -L, for the mirror's reason reached from the far side.

    The books *are* symlinks. Dereferencing on the way in would replace 20.8k links with a
    second literal copy of the vault — twice the disk, and the content-addressed store gone
    in the process. `-a` implies `-l`, which is what recreates them as links; verified
    against sigmaai.au, where the one new book arrived as
    `cL … -> ../../../.data/53f8…`.
    """
    argv = _pull(app, settings)
    assert "-L" not in argv
    assert "-a" in argv


def test_a_pull_does_not_relativise_its_remote_source(app, settings):
    """No -R, and this is the one that fails silently in the worst direction.

    -R sends the source path as written, so with a *remote* source it makes the remote's
    own path a component of the destination. Measured 2026-09-14 against sigmaai.au:

        rsync -a -O -n -i -R --exclude=/.data/ … tigran@sigmaai.au:/Books/ /Books/
        cd+++++++++ Books/
        cd+++++++++ Books/.data/
        >f+++++++++ Books/.data/00001b57bae9…          (and all 20,793 blobs)

    A whole second library at /Books/Books/, every symlink in it dangling because
    `../../.data/<blob>` no longer resolves — and it broke the exclude's anchoring on the
    way past. No error, no warning.
    """
    assert "-R" not in _pull(app, settings)
    # And the push still has it, because there it is what keeps a selection's shape.
    push = build_argv(
        _device(app, "kobo"), app.state.lib.devices.config, ["Science"], settings
    )
    assert "-R" in push


def test_a_pull_can_never_delete(app, settings, library):
    """Not conditional, not a setting: absent, in every leg and both modes.

    `--del`, `--delete-during` and `--delete-excluded` all begin the same way, so the
    assertion is on the prefix rather than the exact flag.
    """
    settings.catalog_db = library / ".data" / "db" / "lib.db"
    for argv in (
        _pull(app, settings),
        _pull(app, settings, dry_run=True),
        build_catalog_argv(_device(app, "source"), app.state.lib.devices.config, settings),
    ):
        assert not any(a.startswith("--del") for a in argv), argv


def test_a_pull_resumes_without_ever_naming_a_partial_blob(app, settings):
    """--partial-dir, not --partial, and the difference is a corrupt vault.

    On interruption plain --partial renames the partial file to its *final* name. On a
    device that is an accepted cost; in `.data/` it is a blob whose contents do not hash to
    the blake2b name it is sitting under, and every symlink pointing at it serves a
    truncated book until something notices.
    """
    argv = _pull(app, settings)
    assert "--partial" not in argv
    assert any(a.startswith("--partial-dir=") for a in argv)


def test_a_pull_keeps_the_flags_the_progress_parser_reads(app, settings):
    """The dock, the log and the SSE fan-out are untouched by this feature, and this is
    why: rsync is asked for exactly the same output in both directions."""
    from libnodes.jobs import INFO_FLAGS, OUT_FORMAT

    argv = _pull(app, settings)
    assert f"--info={INFO_FLAGS}" in argv
    assert f"--out-format={OUT_FORMAT}" in argv


def test_a_pull_leaves_the_device_as_destination_flags_behind(app, settings):
    """None of them, on a node that declares every reason to want them.

    The fixture upstream says `fs: vfat` and `stores_times: false` precisely so this cannot
    pass by luck: these flags are facts about the *device as a destination*, and on a pull
    the destination is this host's ext4. They are not emitted because build_pull_argv has
    no branch that could emit them.
    """
    argv = _pull(app, settings)
    for flag in ("--no-perms", "--no-owner", "--no-group", "--size-only", "--no-times"):
        assert flag not in argv, flag
    assert not any(a.startswith("--modify-window") for a in argv)
    # And the push to that same declaration still gets all of them, so the absence above
    # is about direction and not about the fixture being quiet.
    push = build_argv(
        _device(app, "kobo"), app.state.lib.devices.config, ["Science"], settings
    )
    assert "--no-perms" in push


def test_the_remote_is_the_source_and_the_library_is_the_destination(app, settings):
    argv = _pull(app, settings)
    assert argv[-2] == "tigran@127.0.0.1:/Books/"
    assert argv[-1] == f"{str(settings.library_root).rstrip('/')}/"


def test_a_pull_withholds_the_app_tree_and_the_staging_area(app, settings):
    """Anchored, because the transfer root *is* the library root."""
    argv = _pull(app, settings)
    assert "--exclude=/urantia-library/" in argv
    assert "--exclude=/.data/staging/" in argv


def test_the_vault_itself_is_pulled(app, settings):
    """The half that is easy to over-exclude.

    PULL_EXCLUDES is not SKIP_TOPLEVEL and must not be collapsed into it: a pull *wants*
    `.data/`, which is the vault every incoming symlink resolves into, and wants
    `Recommended/`, whose companion links cost a few hundred bytes with no -L to expand
    them. Only two of the skiplist's nine names appear in the pull's.
    """
    argv = _pull(app, settings)
    assert "--exclude=/.data/" not in argv
    assert not any(a == "--exclude=/Recommended/" for a in argv)
    assert "/.data/staging/" in PULL_EXCLUDES and "/Recommended/" not in PULL_EXCLUDES


def test_the_live_pass_skips_only_the_catalog_files(app, settings, library):
    """Named one by one, not as a directory.

    Excluding `/.data/db/` wholesale would mean anything else that ever lives there is
    never pulled at all, and the whole point of the phasing is to confine the quiet window
    to exactly the file that needs one.
    """
    settings.catalog_db = library / ".data" / "db" / "lib.db"
    argv = _pull(app, settings)
    assert "--exclude=/.data/db/lib.db" in argv
    assert "--exclude=/.data/db/lib.db-wal" in argv
    assert "--exclude=/.data/db/lib.db-shm" in argv
    assert "--exclude=/.data/db/" not in argv


def test_the_catalog_leg_names_one_file_on_each_side(app, settings, library):
    """Which is what makes it a rename as well as a copy."""
    settings.catalog_db = library / ".data" / "db" / "lib.db"
    argv = build_catalog_argv(
        _device(app, "source"), app.state.lib.devices.config, settings
    )
    assert argv[-2].endswith("/Books/.data/db/lib.db.pull-snapshot")
    assert argv[-1] == str(settings.catalog_db)
    assert "-R" not in argv


def test_the_snapshot_runs_on_the_far_end_and_never_stops_it(app, settings, library):
    """`Connection.backup` reads a live WAL database without blocking its writer, which is
    the whole reason production is never taken down for a pull. Measured against the live
    catalog on sigmaai.au with the service serving: 82 tables, 7,224 pages, 1.07 s."""
    settings.catalog_db = library / ".data" / "db" / "lib.db"
    argv = snapshot_argv(_device(app, "source"), app.state.lib.devices.config, settings)
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert "backup" in argv[-1]
    assert not any("systemctl" in a for a in argv)


@pytest.mark.parametrize("build", [snapshot_argv, cleanup_argv])
def test_a_remote_command_is_one_already_quoted_word(app, settings, library, build):
    """ssh does not pass argv through, and this is the bug that taught us.

    Everything after `user@host` is joined with single spaces and handed to a shell on the
    far side, so a tidy argv list arrives **unquoted** and is re-split on whitespace. The
    snapshot script went out as a list and came back (job #18, measured):

        File "<string>", line 1
            import
                  ^
        SyntaxError: Expected one or more names after 'import'
        bash: -c: line 2: syntax error near unexpected token `('

    The log was no help either: `_stream` writes the argv back out shlex-quoted, so it
    printed the command as it should have been sent rather than as it was. Hence an
    assertion on the *shape* — exactly one element after the destination — rather than on
    the contents, which were correct the whole time.
    """
    settings.catalog_db = library / ".data" / "db" / "lib.db"
    argv = build(_device(app, "source"), app.state.lib.devices.config, settings)
    dest = argv.index("tigran@127.0.0.1")
    assert argv[dest + 1 :] == [argv[-1]], "ssh re-splits anything more than one word"


@pytest.mark.parametrize(
    "build,head",
    [(snapshot_argv, "python3"), (cleanup_argv, "rm")],
)
def test_a_remote_command_survives_the_shell_that_will_re_split_it(
    app, settings, library, build, head
):
    """The other half: one word is necessary, correct quoting inside it is sufficient.

    A shell parse of what we send has to give back the argv we meant — including the
    newline-free one-liner and its nested quotes. `shlex.split` is the same parse bash
    does for this.
    """
    import shlex

    settings.catalog_db = library / ".data" / "db" / "lib.db"
    argv = build(_device(app, "source"), app.state.lib.devices.config, settings)
    parsed = shlex.split(argv[-1])
    assert parsed[0] == head
    assert all("\n" not in word for word in parsed), "a newline re-splits on the far side"
    if head == "python3":
        # And the script the far end would actually receive is valid Python.
        compile(parsed[2], "<snapshot>", "exec")
        assert parsed[-2].endswith("/lib.db")
        assert parsed[-1].endswith("/lib.db.pull-snapshot")


def test_the_service_commands_never_shell_out_to_sudo(app, settings):
    """deploy/libnodes.service sets NoNewPrivileges=yes, which makes sudo's setuid bit
    inert — it refuses outright and no sudoers rule fixes it. The privilege comes from
    polkit instead, so the unit keeps every constraint it has."""
    settings.local_service = "urantia-library.service"
    argv = service_argv("stop", settings)
    assert argv[0] == "systemctl"
    assert "sudo" not in argv
    assert "--no-ask-password" in argv
    assert argv[-1] == "urantia-library.service"


# ------------------------------------------------------------------ refusals --


def test_build_argv_refuses_to_compose_any_push_to_an_upstream_node(app, settings):
    """The guard no caller can opt out of.

    `JobRunner.submit` composes the argv for every writing path there is, so a route that
    was never taught about upstream — or `retry`, replaying a stored job's sources long
    after the routes were fixed — still cannot get a transfer aimed at the library's
    source.
    """
    with pytest.raises(ValueError, match="pull source"):
        build_argv(
            _device(app, "source"),
            app.state.lib.devices.config,
            mirror_sources(settings),
            settings,
        )


def test_build_pull_argv_refuses_a_node_that_is_not_upstream(app, settings):
    """The reverse lock, and the pair is the point: one function can only aim at a device,
    the other can only read from a source, and neither can be talked into the other's
    direction."""
    for node in ("kobo", "thinkpad"):
        with pytest.raises(ValueError, match="upstream"):
            build_pull_argv(_device(app, node), app.state.lib.devices.config, settings)


def test_submitting_a_push_to_an_upstream_node_raises(app, settings):
    """Proves the refusal reaches JobRunner.submit, and therefore every caller of it."""
    lib = app.state.lib
    with pytest.raises(ValueError):
        lib.jobs.submit(_device(app, "source"), ["Science"])


@pytest.mark.parametrize("action", ["replicate", "full-sync", "adopt", "dry-run"])
async def test_no_writing_route_is_a_way_into_an_upstream_node(client, action):
    """Adopt is in this list because it had no mode guard at all — it never asked what the
    device was, which is how it stayed the one writing endpoint that would still reach
    production after every other route had been taught to refuse. `--size-only` makes it
    quieter, not read-only."""
    r = await client.post(f"/device/source/{action}")
    assert r.status_code == 404, action


async def test_the_mirror_keeps_every_action_the_upstream_lost(client):
    """The refusals above must be about `upstream`, not about tightening the app."""
    for action in ("replicate", "adopt", "dry-run"):
        r = await client.post(f"/device/thinkpad/{action}")
        assert r.status_code != 404, action


async def test_a_selection_post_to_an_upstream_node_is_refused(client):
    """A hidden button is not a guard: this is a form post."""
    r = await client.post("/jobs", data={"device": "source", "path": ["Science"]})
    assert r.status_code == 200
    assert "upstream" in r.text.lower()


async def test_an_upstream_node_is_not_a_selection_target(client):
    """Absent from the picker, and from the Library view's row buttons."""
    picker = await client.get("/jobs/picker?path=Science")
    assert 'value="source"' not in picker.text
    assert 'value="kobo"' in picker.text
    lib = await client.get("/library")
    assert "Test Upstream" not in lib.text


async def test_the_menu_offers_pull_to_an_upstream_and_never_replicate(client):
    """Compared by endpoint rather than by label, because that is what an action is."""
    menu = await client.get("/device/source/menu")
    assert "/device/source/pull" in menu.text
    assert "/device/source/pull-dry-run" in menu.text
    assert "/device/source/replicate" not in menu.text
    assert "/device/source/full-sync" not in menu.text
    assert "/device/source/adopt" not in menu.text
    # Scan survives: it reads, and on an upstream it is the action that matters most,
    # because the backlog is derived from it.
    assert "/device/source/scan" in menu.text


async def test_a_full_sync_true_upstream_is_still_not_offered_full_sync(client, app):
    """The trap this feature was one line away from shipping.

    `full_sync` is mutually exclusive with *mirror* — by an explicit term in the route and
    an `elif` in the template — and with nothing else. `upstream` is a value that `elif`
    knew nothing about, so declaring it without removing the key would have put Full Sync,
    a push to production, straight back in the menu. Three guards answer it; this asserts
    the first, which coerces the key off at parse time.
    """
    assert _device(app, "source").full_sync is False
    menu = await client.get("/device/source/menu")
    assert "Full Sync" not in menu.text


async def test_the_menu_shows_every_step_of_a_pull_not_just_the_transfer(client, app, settings, library):
    """The hazard in a Pull is not the rsync — it has no --delete and no -L and writes
    nothing to the far end. It is `systemctl stop` and an overwritten catalog."""
    settings.catalog_db = library / ".data" / "db" / "lib.db"
    settings.local_service = "urantia-library.service"
    menu = await client.get("/device/source/menu")
    for fragment in ("systemctl", "backup", "rm -f", "lib.db"):
        assert fragment in menu.text, fragment


# ---------------------------------------------------------------------- scan --


def test_an_upstream_scan_asks_rsync_for_link_targets_like_a_mirrors_does(app, settings):
    """`cas_tree`, not `is_mirror`, and missing this fails *green*.

    Every book on an upstream is a symlink. A scan that drops links keeps only the vault
    rows, and `expected_toplevel` then filters those away — so the dialog would report a
    full production library as an empty backlog, with the suite passing and the page
    looking plausible.
    """
    assert "-l" in scan_argv(_device(app, "source"), settings)
    assert "-l" in scan_argv(_device(app, "thinkpad"), settings)
    assert "-l" not in scan_argv(_device(app, "kobo"), settings)


async def test_an_upstreams_vault_is_not_reported_as_orphans(client, app):
    """A correct upstream holds `.data/` and `urantia-library/`, neither of which the index
    has ever heard of. Without the skiplist the dialog invites you to delete ~24.6k blobs
    from a node you cannot delete anything on anyway."""
    lib = app.state.lib
    lib.manifests.replace_scan(
        "source",
        [
            (".data/" + "a" * 64, None, 4096, 1, 0),
            ("urantia-library/webapp/main.py", None, 100, 1, 0),
            ("Science/New-Book.pdf", "b" * 64, 0, 1, 0),
        ],
    )
    r = await client.get("/device/source/extras")
    assert "New-Book.pdf" in r.text
    assert "urantia-library/webapp" not in r.text
    assert "a" * 64 not in r.text


async def test_an_upstream_backlog_reports_book_sizes_rather_than_zero(client, app):
    """A scanned symlink carries a hash and size 0 (the link's own bytes would be a lie
    about the book). The same scan lists the vault, so the real size is already in the
    table under `.data/<hash>` — resolve through it. A zero is a claim."""
    blob = "c" * 64
    lib = app.state.lib
    lib.manifests.replace_scan(
        "source",
        [
            (f".data/{blob}", None, 92_593_727, 1, 0),
            ("Science/Vegener.pdf", blob, 0, 1, 0),
        ],
    )
    r = await client.get("/device/source/extras")
    assert "88.3 MB" in r.text
    assert "0 B" not in r.text


async def test_an_unresolvable_size_reads_as_unknown_rather_than_zero_bytes(client, app):
    """A dash is an admission; a zero is a claim about the book."""
    lib = app.state.lib
    lib.manifests.replace_scan(
        "source", [("Science/Orphan.pdf", "d" * 64, 0, 1, 0)]
    )
    r = await client.get("/device/source/extras")
    assert "Orphan.pdf" in r.text
    assert "—" in r.text
    # And the footer does not quietly total the dashes into a claim of emptiness.
    assert "0 B listed" not in r.text
    assert "1 of unknown size" in r.text


async def test_the_backlog_says_which_of_it_a_pull_will_decline(client, app):
    """`a Pull brings exactly this across` was true until the excludes existed. On
    sigmaai.au 56 of the 56.5 GB listed is `Unsorted/`, which the pull holds back."""
    lib = app.state.lib
    lib.manifests.replace_scan(
        "source",
        [
            ("Science/Wanted.pdf", None, 10, 1, 0),
            ("Unsorted/Big.img", None, 20, 1, 0),
            ("urantia-library/secrets.env", None, 30, 1, 0),
        ],
    )
    r = await client.get("/device/source/extras")
    assert "held back" in r.text
    assert "exactly this across" not in r.text


# -------------------------------------------------------------------- badges --


async def test_the_row_and_the_card_badge_an_upstream_node(client):
    """One node in this fleet must never be written to, and it reads the same as a mirror
    in the yaml. It must not read the same on the page."""
    for view in ("/devices/rows", "/devices/grid"):
        page = await client.get(view)
        assert "UPSTREAM" in page.text, view
        assert "MIRROR" in page.text, view


# ------------------------------------------------------------- the six phases --


@pytest.fixture
def trace(tmp_path: Path) -> Path:
    """Where the fake commands record that they ran, in order."""
    return tmp_path / "trace.txt"


def _recorder(tmp_path: Path, trace: Path, name: str, exit_code: int = 0) -> list[str]:
    """A stand-in command that appends its own name to the trace and exits as told.

    Order is the thing being asserted in most of these — that the stale write-ahead log is
    gone before the new catalog lands, that `start` follows `stop` whatever happened in
    between — so a recorder that keeps the sequence is worth more than a mock that counts
    calls.
    """
    script = tmp_path / f"fake-{name}"
    script.write_text(
        "#!/bin/sh\n"
        f"echo {name} >> {trace}\n"
        f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    return [str(script)]


@pytest.fixture
def pull_rig(monkeypatch, app, settings, library, tmp_path, trace):
    """Patch all five argv builders the runner calls, and hand back a knob per phase.

    The builders are resolved from `libnodes.jobs`'s module globals at call time, which is
    what makes this work — and what makes the real ones worth testing separately, above.
    """
    import libnodes.jobs as J

    settings.catalog_db = library / ".data" / "db" / "lib.db"
    settings.catalog_db.parent.mkdir(parents=True, exist_ok=True)
    settings.catalog_db.write_text("old catalog", encoding="utf-8")
    settings.local_service = "fake.service"

    codes = {"pull": 0, "snapshot": 0, "stop": 0, "catalog": 0, "start": 0, "cleanup": 0}

    def install():
        monkeypatch.setattr(
            J, "build_pull_argv",
            lambda *a, **k: _recorder(tmp_path, trace, "pull", codes["pull"]))
        monkeypatch.setattr(
            J, "snapshot_argv",
            lambda *a, **k: _recorder(tmp_path, trace, "snapshot", codes["snapshot"]))
        monkeypatch.setattr(
            J, "build_catalog_argv",
            lambda *a, **k: _recorder(tmp_path, trace, "catalog", codes["catalog"]))
        monkeypatch.setattr(
            J, "cleanup_argv",
            lambda *a, **k: _recorder(tmp_path, trace, "cleanup", codes["cleanup"]))
        monkeypatch.setattr(
            J, "service_argv",
            lambda verb, s: _recorder(tmp_path, trace, verb, codes[verb]))

    async def run(dry_run: bool = False):
        install()
        lib = app.state.lib
        job = lib.jobs.submit_pull(_device(app, "source"), dry_run=dry_run)
        await lib.jobs._run(job.id)
        return lib.jobs.get(job.id)

    def steps() -> list[str]:
        return trace.read_text(encoding="utf-8").split() if trace.exists() else []

    rig = type("Rig", (), {})()
    rig.codes, rig.run, rig.steps, rig.settings = codes, run, steps, settings
    return rig


async def test_a_clean_pull_runs_all_six_phases_in_order(pull_rig):
    job = await pull_rig.run()
    assert pull_rig.steps() == [
        "pull", "snapshot", "stop", "catalog", "start", "cleanup"
    ]
    assert job.state == "done"


async def test_a_dry_pull_takes_nothing_down_and_writes_no_snapshot(pull_rig):
    """Checked *before* the snapshot, not after: a preview must never write a file onto
    the upstream and must never stop a service."""
    job = await pull_rig.run(dry_run=True)
    assert pull_rig.steps() == ["pull"]
    assert job.state == "done"


async def test_a_pull_restarts_the_local_service_even_when_the_catalog_fails(pull_rig):
    """The `finally` is the reason the six phases live in one coroutine: a try/finally
    cannot span two jobs."""
    pull_rig.codes["catalog"] = 1
    job = await pull_rig.run()
    assert pull_rig.steps() == [
        "pull", "snapshot", "stop", "catalog", "start", "cleanup"
    ]
    assert "catalog" in (job.catalog_warning or "").lower()


async def test_a_pull_that_never_stopped_the_service_never_starts_it(pull_rig):
    """A failure before the stop must not `systemctl start` something the operator had
    deliberately stopped."""
    pull_rig.codes["stop"] = 1
    await pull_rig.run()
    steps = pull_rig.steps()
    assert "stop" in steps
    assert "start" not in steps
    assert "catalog" not in steps


async def test_a_failed_snapshot_leaves_the_books_but_says_the_catalog_is_stale(pull_rig):
    """Amber, not green and not red: the transfer really did land. A green banner over a
    stale catalog is the same small lie as a plain SYNC COMPLETE over exit 23."""
    pull_rig.codes["snapshot"] = 1
    job = await pull_rig.run()
    steps = pull_rig.steps()
    assert steps == ["pull", "snapshot", "cleanup"]
    assert job.state == "done"
    assert job.catalog_warning


async def test_a_failed_transfer_never_reaches_the_catalog_at_all(pull_rig):
    pull_rig.codes["pull"] = 1
    job = await pull_rig.run()
    assert pull_rig.steps() == ["pull"]
    assert job.state == "failed"


async def test_the_stale_write_ahead_log_is_gone_before_the_new_catalog_lands(pull_rig):
    """Order is where the corruption lives: applying a WAL belonging to the old file over
    a fresh one is the one way this loses a catalog rather than merely failing."""
    wal = Path(str(pull_rig.settings.catalog_db) + "-wal")
    shm = Path(str(pull_rig.settings.catalog_db) + "-shm")
    wal.write_text("stale", encoding="utf-8")
    shm.write_text("stale", encoding="utf-8")
    await pull_rig.run()
    assert not wal.exists()
    assert not shm.exists()


async def test_the_snapshot_is_removed_from_the_upstream_even_when_a_phase_fails(pull_rig):
    """An abandoned snapshot is 30 MB of somebody else's disk, and it would turn up in the
    next scan's backlog — the list that is supposed to mean "books you have not pulled"."""
    pull_rig.codes["catalog"] = 1
    await pull_rig.run()
    assert pull_rig.steps()[-1] == "cleanup"


async def test_a_pull_writes_no_manifest_row_for_the_upstream_node(pull_rig, app):
    """`_update_manifest` records what a *device* holds by walking the local index, which
    after a pull is inverted — and it would run before the reindex, so it would record the
    pre-pull index as a claim about the far end."""
    lib = app.state.lib
    await pull_rig.run()
    assert lib.manifests.summary("source")[0] == 0


async def test_a_pull_asks_for_a_reindex_and_a_push_does_not(pull_rig, app, monkeypatch):
    """The only thing in the program that reindexes because of an event rather than a
    schedule, because it is the only job that writes into library_root."""
    calls: list[int] = []
    monkeypatch.setattr(app.state.lib.jobs, "_on_library_changed", lambda: calls.append(1))
    await pull_rig.run()
    assert calls == [1]


async def test_an_interrupted_pull_still_asks_for_a_reindex(pull_rig, app, monkeypatch):
    """It has still written files, and an index that does not know about them makes those
    books invisible in the Library view *and* unpushable — `_resolve` admits only what the
    index vouches for."""
    calls: list[int] = []
    monkeypatch.setattr(app.state.lib.jobs, "_on_library_changed", lambda: calls.append(1))
    pull_rig.codes["pull"] = 1
    await pull_rig.run()
    assert calls == [1]


async def test_a_dry_pull_asks_for_nothing(pull_rig, app, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(app.state.lib.jobs, "_on_library_changed", lambda: calls.append(1))
    await pull_rig.run(dry_run=True)
    assert calls == []


async def test_a_pull_retries_its_transfer_but_not_its_catalog_swap(
    pull_rig, app, monkeypatch
):
    """The fleet's `retries: 2` past the stop would mean three stop/start cycles of the
    local service chasing a failure a human needs to look at. Inside phase 1 a retry is
    free — `--partial-dir` resumes byte-accurate and nothing has been taken down.

    The retry budget is forced on here rather than taken from the fixture, whose
    `retries: 0` would let this pass without testing anything.
    """
    monkeypatch.setattr(app.state.lib.jobs, "_retries_for", lambda job: 1)

    pull_rig.codes["pull"] = 1
    job = await pull_rig.run()
    assert job.state == "queued", "a failed transfer should have been requeued"

    # A failure past the stop must not be, however much budget is left.
    pull_rig.codes["pull"] = 0
    pull_rig.codes["catalog"] = 1
    job = await pull_rig.run()
    assert job.state == "done" and job.catalog_warning
    assert job.state != "queued"


async def test_a_restart_during_the_quiet_window_starts_the_service_again(
    app, settings, tmp_path, monkeypatch
):
    """The hole a `finally` cannot close. `JobRunner.stop()` cancels the workers, and
    `sudo systemctl restart libnodes` is the routine dev loop on this host — land one of
    those between the stop and the start and the site stays down with nothing running to
    bring it back. The durable half is a file, read by `start()`."""
    import libnodes.jobs as J

    ran: list[list[str]] = []
    monkeypatch.setattr(
        J.subprocess, "run", lambda argv, **kw: ran.append(argv) or None
    )
    lib = app.state.lib
    hold = lib.jobs._service_hold
    hold.parent.mkdir(parents=True, exist_ok=True)
    hold.write_text('{"unit": "fake.service", "job": 1, "at": 0}', encoding="utf-8")

    lib.jobs.start()

    assert ran and ran[0] == ["systemctl", "--no-ask-password", "start", "fake.service"]
    assert not hold.exists(), "the breadcrumb must not survive its own recovery"


async def test_a_clean_pull_leaves_no_breadcrumb_behind(pull_rig, app):
    """Otherwise every subsequent restart would start a service nobody had stopped."""
    await pull_rig.run()
    assert not app.state.lib.jobs._service_hold.exists()
