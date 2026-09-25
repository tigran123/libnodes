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
import os
import shlex
from pathlib import Path
from typing import Sequence

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


def test_a_pull_prunes_what_the_upstream_no_longer_has(app, settings):
    """--delete, in both modes, because a replica that only grows is not a replica.

    This asserted the opposite for a year, and the invariant it pinned was wrong rather
    than merely cautious: an upstream is the library's source of truth, so a book it
    deletes is a book that should go. Without the flag /Books here kept the stale symlink,
    its blob and its cover for ever, and the Library view offered a retired book to every
    device in the fleet. Measured against sigmaai.au on 2026-09-19, after four months of
    pulls: three objects, `Number of created files: 0`.

    Under -n as well, deliberately: a mirror's dry run is the only preview of its prune
    (CLAUDE.md) and a pull's is now the only preview of this one.
    """
    for argv in (_pull(app, settings), _pull(app, settings, dry_run=True)):
        assert "--delete" in argv, argv


def test_the_catalog_leg_still_cannot_delete(app, settings, library):
    """One file named on both sides has nothing a --delete could mean.

    `--del`, `--delete-during` and `--delete-excluded` all begin the same way, so the
    assertion is on the prefix rather than the exact flag — and it stays on the prefix now
    that its sibling leg carries the real thing.
    """
    settings.catalog_db = library / ".data" / "db" / "lib.db"
    argv = build_catalog_argv(
        _device(app, "source"), app.state.lib.devices.config, settings
    )
    assert not any(a.startswith("--del") for a in argv), argv


def test_the_prune_is_capped_and_the_cap_is_a_setting(app, settings):
    """--max-delete, from Settings.pull_max_delete, and a negative value removes it.

    The failure this exists for is an upstream that is only half there: an unmounted
    /Books presents an almost empty file list, and the honest reading of that is "delete
    everything" — 63,518 entries of a correct library in one pass. Hitting the cap is
    rsync exit 25, which stops the deletions and keeps the files it received.

    Zero cannot mean "uncapped" because zero is rsync's own useful setting: delete
    nothing, but exit 25 if anything would have been. So the opt-out is negative.
    """
    assert "--max-delete=1000" in _pull(app, settings)

    settings.pull_max_delete = 7
    assert "--max-delete=7" in _pull(app, settings)

    settings.pull_max_delete = 0
    assert "--max-delete=0" in _pull(app, settings)

    settings.pull_max_delete = -1
    argv = _pull(app, settings)
    assert not any(a.startswith("--max-delete") for a in argv), argv
    assert "--delete" in argv


def test_the_excluded_trees_are_not_pruned(app, settings, library):
    """The excludes are what keep --delete from being a whole-library prune.

    rsync does not delete what an --exclude matched, so the three boundaries PULL_EXCLUDES
    draws hold in the delete direction too without a second rule saying so: this host's own
    urantia-library/ (its secrets.env is per-host), the staging area (a torn blob would not
    hash to its own name) and Unsorted/. Confirmed by the dry run that produced the three
    deletions above — /Unsorted/ is 55 GB and was not among them.

    Asserted as coexistence rather than as behaviour, because the behaviour belongs to
    rsync: what this file can pin is that we still send both.
    """
    settings.catalog_db = library / ".data" / "db" / "lib.db"
    argv = _pull(app, settings)
    assert "--delete" in argv
    for pattern in PULL_EXCLUDES:
        assert f"--exclude={pattern}" in argv, argv
    # And the catalog, which a prune must not reach either: it is swapped in by its own
    # phase, under a stopped reader.
    assert any(a.startswith("--exclude=/") and a.endswith("lib.db") for a in argv), argv


def test_the_cap_names_itself_when_it_fires(app):
    """Exit 25 is a refusal, and the hint has to say which knob refused.

    rsync's own wording is the key, because the number alone says nothing: 25 is
    "--max-delete limit stopped deletions" and the first question is always whether the
    removals were real or the upstream was half mounted.
    """
    from libnodes.jobs import hints_for_text

    hints = hints_for_text(
        "rsync warning: Deletions stopped due to --max-delete limit (1 skipped)\n", 25
    )
    assert any("LIBNODES_PULL_MAX_DELETE" in h for h in hints), hints


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
    """Absent from the picker, which is now the only way a push starts in the Library.

    It is *present* in every row's presence map, which is not a contradiction: the map
    says who holds a book, and what the library's own source holds is the most worth
    knowing of all. The map offers no action, so naming a node there is not a way into
    it -- both of a row's buttons open the picker, and the picker is where the filter is.
    """
    picker = await client.get("/jobs/picker?path=Science")
    assert 'value="source"' not in picker.text
    assert 'value="kobo"' in picker.text
    lib = await client.get("/library")
    assert 'hx-post="/jobs"' not in lib.text


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


def _recorder(
    tmp_path: Path,
    trace: Path,
    name: str,
    exit_code: int = 0,
    emits: Sequence[str] = (),
) -> list[str]:
    """A stand-in command that appends its own name to the trace and exits as told.

    Order is the thing being asserted in most of these — that the stale write-ahead log is
    gone before the new catalog lands, that `start` follows `stop` whatever happened in
    between — so a recorder that keeps the sequence is worth more than a mock that counts
    calls.

    `emits` writes lines to stdout first, so a phase can hand the runner the genuine
    `@%l|%n` output the parser reads. That is how `_credit_pull` is exercised: what a pull
    records is a function of what rsync said it received.
    """
    script = tmp_path / f"fake-{name}"
    body = "".join(f"printf '%s\\n' {shlex.quote(line)}\n" for line in emits)
    script.write_text(
        "#!/bin/sh\n"
        f"echo {name} >> {trace}\n"
        f"{body}"
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
    #: Lines a phase prints. `pull` is the @-lines, i.e. what the upstream sent us; any
    #: other phase can be given its own, which is how the later ones are shown *not* to
    #: redefine the transfer's numbers.
    emits: dict[str, list[str]] = {"pull": []}

    def install():
        monkeypatch.setattr(
            J, "build_pull_argv",
            lambda *a, **k: _recorder(
                tmp_path, trace, "pull", codes["pull"], emits["pull"]))
        monkeypatch.setattr(
            J, "snapshot_argv",
            lambda *a, **k: _recorder(tmp_path, trace, "snapshot", codes["snapshot"],
                                      emits.get("snapshot", ())))
        monkeypatch.setattr(
            J, "build_catalog_argv",
            lambda *a, **k: _recorder(tmp_path, trace, "catalog", codes["catalog"],
                                      emits.get("catalog", ())))
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
    rig.emits = emits
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


def _blob_of(settings, rel: str) -> str:
    return os.path.basename(os.readlink(settings.library_root / rel))


async def test_a_pull_credits_the_upstream_with_what_it_received(pull_rig, app, settings):
    """PRESENT ON went blank on a book we had just pulled *from* that node, and stayed
    blank until somebody ran a scan — sigmaai.au's last scan predated the file by three
    days. A file we received from a node is a file that node has, which is the strongest
    evidence of the three PRESENT ON draws on and the only one a pull generates.

    Not `_update_manifest`: that walks the *local* index for each source, which after a
    pull is inverted in direction, and it runs before the reindex. This reads the @-lines
    and then the filesystem.
    """
    lib = app.state.lib
    blob = _blob_of(settings, "Science/Physics/Feynman.djvu")
    pull_rig.emits["pull"] = [
        f"@720|.data/{blob}",
        "@140|Science/Physics/Feynman.djvu",
    ]
    await pull_rig.run()

    rows = {r.path: r for r in lib.manifests.rows_for("source")}
    assert set(rows) == {"Science/Physics/Feynman.djvu"}, (
        "the vault is not a library row: `.data/` is in SKIP_TOPLEVEL and no PRESENT ON "
        "chip is ever about a blob"
    )
    row = rows["Science/Physics/Feynman.djvu"]
    assert row.source == "pull"
    assert row.blob == blob, (
        "a CAS node's row carries the blake2b out of the link target, which is a content "
        "claim rather than the size guess a listing settles for"
    )


async def test_a_pull_credits_only_what_actually_landed(pull_rig, app, settings):
    """rsync prints a name when it *starts* sending it, and `--partial-dir` keeps an
    interrupted file out of its final name. So the filesystem decides, not the @-line —
    which is what makes this safe to run on an abort as well as a clean exit."""
    lib = app.state.lib
    pull_rig.emits["pull"] = [
        "@140|Science/Physics/Feynman.djvu",
        "@999|Fiction/Never-Arrived.epub",
    ]
    await pull_rig.run()
    assert lib.manifests.paths_for("source") == {"Science/Physics/Feynman.djvu"}


async def test_an_interrupted_pull_still_credits_what_it_brought(pull_rig, app, settings):
    """Every terminal outcome, for the same reason the reindex runs on all of them: the
    books are here and the node that sent them still holds them."""
    lib = app.state.lib
    pull_rig.emits["pull"] = ["@140|Science/Physics/Feynman.djvu"]
    pull_rig.codes["pull"] = 1
    job = await pull_rig.run()
    assert job.state == "failed"
    assert lib.manifests.paths_for("source") == {"Science/Physics/Feynman.djvu"}


async def test_a_previewed_pull_credits_nothing(pull_rig, app, settings):
    """A dry run changes nothing anywhere, and a manifest is somewhere."""
    lib = app.state.lib
    pull_rig.emits["pull"] = ["@140|Science/Physics/Feynman.djvu"]
    await pull_rig.run(dry_run=True)
    assert lib.manifests.rows_for("source") == []


async def test_a_pull_counts_and_retracts_what_it_pruned(pull_rig, app, settings):
    """The prune is evidence in its own right, and the manifest has to hear it.

    A `deleting` line fires *after* the unlink, so unlike a credit this needs no
    filesystem check: the upstream no longer has the file, and the row saying it does is
    wrong from that moment. Left standing it is not merely untidy — `presence` counts a
    directory's files as a range over `(device_id, path)`, so a stale row adds to the
    numerator of a fraction whose denominator has just lost one.

    The directory row is in the fixture on purpose. rsync removes a directory once its
    contents have gone and announces that the same way, and counting it under a heading
    that says FILES is the `to-chk` mistake again.
    """
    lib = app.state.lib
    lib.manifests.record(
        "source",
        [
            ("Science/Klassiki-Nauki/Clifford/Common-Sense-1946.pdf", "cd61", 9, 1, 0),
            ("Science/Physics/Feynman.djvu", "abc", 12, 1, 0),
        ],
        source="pull",
    )
    pull_rig.emits["pull"] = [
        "@140|Science/Physics/Feynman.djvu",
        "deleting Science/Klassiki-Nauki/Clifford/Common-Sense-1946.pdf",
        "deleting .data/cd61ce843004fc53c9432f0dfc42ffc491366e3c4584447c6c3fa9d5229ab59b",
        "deleting Science/Klassiki-Nauki/Clifford/",
    ]
    job = await pull_rig.run()

    assert job.files_deleted == 2, (
        "two files and one directory: the directory is announced the same way and is not "
        "a file"
    )
    assert lib.jobs.store.get(job.id).files_deleted == 2, (
        "through the store, not just the live object: history has to be able to say that "
        "a job deleted something"
    )
    assert lib.manifests.paths_for("source") == {"Science/Physics/Feynman.djvu"}


async def test_a_deletion_glued_to_a_progress_line_is_still_seen(
    pull_rig, app, settings
):
    """rsync does not always terminate a progress line before its next message.

    Measured against sigmaai.au: of the three deletions in a real pull, only the first
    arrived on a line of its own. The other two came out as

        0   0%    0.00kB/s    0:00:00 (xfr#0, ir-chk=18084/38898)deleting .data/…

    with no CR and no LF between them — so `_iter_lines` had nothing to split on, and
    PROGRESS_RE is unanchored at its end, so the whole string matched as progress and the
    deletion was invisible to everything downstream. The first test written for this
    feature emitted tidy one-per-line output and passed over the bug.

    The `printf` here carries no newline before `deleting`, on purpose: that is the shape
    being pinned, and a fixture that tidies it up tests nothing.
    """
    lib = app.state.lib
    lib.manifests.record(
        "source", [("Science/Physics/Feynman.djvu", "abc", 12, 1, 0)], source="pull"
    )
    pull_rig.emits["pull"] = [
        "0   0%    0.00kB/s    0:00:00 (xfr#0, ir-chk=18084/38898)"
        "deleting Science/Physics/Feynman.djvu",
    ]
    job = await pull_rig.run()

    assert job.files_deleted == 1
    assert lib.manifests.paths_for("source") == set()


async def test_a_previewed_pull_retracts_nothing(pull_rig, app, settings):
    """`-n` prints every `deleting` line it would have run, which is the whole point of
    the preview — and not one of them has happened."""
    lib = app.state.lib
    lib.manifests.record(
        "source", [("Science/Physics/Feynman.djvu", "abc", 12, 1, 0)], source="pull"
    )
    pull_rig.emits["pull"] = ["deleting Science/Physics/Feynman.djvu"]
    await pull_rig.run(dry_run=True)
    assert lib.manifests.paths_for("source") == {"Science/Physics/Feynman.djvu"}


async def test_the_catalog_phase_does_not_redefine_the_transfers_numbers(
    pull_rig, app, settings
):
    """Six phases, one set of counters, and every one of them an assignment.

    Job #31 was a pull whose library phase moved 15,263,475 bytes over the wire across
    63,518 file-list entries. It was recorded as `bytes_wire=445,181`,
    `bytes_done=29,663,232`, `entries=1/1` — the 29.7 MB catalog swap four phases later,
    described as though it were the transfer, in the Jobs table's BYTES column. `track`
    is what confines the figures to the phase that is actually moving the library.
    """
    pull_rig.emits["pull"] = [
        "@140|Science/Physics/Feynman.djvu",
        "5,589,865   0%    7.20MB/s    0:00:00 (xfr#2, to-chk=0/63518)",
        "sent 4,784 bytes  received 15,263,475 bytes  10,178,839.33 bytes/sec",
    ]
    pull_rig.emits["catalog"] = [
        "29,663,232 100%  456.28MB/s    0:00:00 (xfr#1, to-chk=0/1)",
        "sent 32,765 bytes  received 412,416 bytes  890,362.00 bytes/sec",
    ]
    job = await pull_rig.run()

    assert job.state == "done"
    assert job.bytes_wire == 4_784 + 15_263_475
    assert job.bytes_done == 5_589_865
    assert job.entries_total == 63_518
    assert job.files_sent == 2


async def test_a_scan_retracts_what_a_pull_claimed(app, settings):
    """A pull row was true when it was written and nothing else would ever take it back:
    delete the book upstream and it would read present for ever. A scan has just looked,
    so it overrules the transfer — while push rows, which are a claim about a device we
    write *to*, survive as they always have."""
    m = app.state.lib.manifests
    m.record("source", [("Science/Physics/Feynman.djvu", "abc", 12, 1, 0)], source="pull")
    m.record("kobo", [("Science/Physics/Feynman.djvu", "abc", 12, 1, 0)], source="push")

    m.replace_scan("source", [("Fiction/Joyce/Ulysses.pdf", "def", 34, 2, 0)])

    assert m.paths_for("source") == {"Fiction/Joyce/Ulysses.pdf"}
    assert m.paths_for("kobo") == {"Science/Physics/Feynman.djvu"}


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
