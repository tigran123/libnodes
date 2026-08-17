"""`sync_mode: mirror` — the second shape of a push, and everything it inverts.

A reader wants the books: `-L` dereferences the CAS symlinks and the infrastructure
top-level directories are never named. A mirror wants the tree: no `-L`, `.data/` carried
so the surviving symlinks resolve, `urantia-library/` included, and `--delete` because a
replica that keeps what the origin dropped is not one.

Every assertion here is paired with its books-mode counterpart, because each of these was
an invariant before it was a mode: the point is not that mirror does something, it is that
the two do opposite things on purpose.

`build_argv` is stubbed out by every runner and lifecycle test, so mirror behaviour has to
be pinned by calling it directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from libnodes.jobs import build_argv, full_sync_sources, mirror_sources
from libnodes.manifests import _compare
from libnodes.scan import parse_line, parse_listing, scan_argv

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def devices_file(settings) -> Path:
    """Overrides the shared fixture: this module needs a mirror node in the fleet.

    Deliberately local. `push_devices` takes the first two *selectable* devices and
    several UI tests elsewhere count on the shared fleet being exactly what it is.
    """
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
    capacity: 29G

  - id: phone
    name: Test Phone
    abbr: PH
    type: termux
    host: 127.0.0.1
    port: 8022
    user: u0_a1
    target: /sdcard/Books

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
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _device(app, device_id):
    return app.state.lib.devices.config.by_id[device_id]


def _mirror_argv(app, settings, **kw):
    lib = app.state.lib
    return build_argv(
        _device(app, "thinkpad"),
        lib.devices.config,
        mirror_sources(settings),
        settings,
        **kw,
    )


# ------------------------------------------------------------------- the flags --


def test_the_default_is_books_so_nothing_moved_under_the_fleet(app):
    """A devices.yaml that says nothing about the mode gets the behaviour it always had."""
    assert _device(app, "kobo").sync_mode == "books"
    assert _device(app, "kobo").is_mirror is False
    assert _device(app, "thinkpad").is_mirror is True


def test_a_books_push_still_forces_copy_links(app, settings):
    """The invariant, restated beside its one exception.

    Stated here as well as in test_jobs.py because this is the file that introduces a way
    for it to be false, and the pair has to be readable in one place.
    """
    lib = app.state.lib
    argv = build_argv(_device(app, "kobo"), lib.devices.config, ["Science"], settings)
    assert "-L" in argv


def test_a_mirror_push_keeps_the_symlinks(app, settings):
    """No -L: on a mirror the links are the thing being replicated, not an obstacle."""
    argv = _mirror_argv(app, settings)
    assert "-L" not in argv
    # Everything else survives. -a still implies -l, which is what recreates them.
    assert "-a" in argv and "-R" in argv and "-O" in argv


def test_a_mirror_sends_the_root_itself_so_the_prune_reaches_the_top_level(app, settings):
    """One source, `./`, and that is what makes --delete mean what it says.

    --delete only prunes directories that are part of the transfer. Enumerate the top-level
    names and rsync cleans inside `Science/` while never scanning the destination root, so a
    stray top-level file outlives every replicate. Measured on a local pair: the enumerated
    form left `Leftover.pdf` and an orphaned `OldCat/` behind, `./` removed both.
    """
    argv = _mirror_argv(app, settings)
    sources = argv[argv.index("-e") + 2 : -1]
    assert sources == ["./"]
    # -R is still what anchors it: `./` plus --relative makes the transfer root the
    # destination root rather than a subdirectory of it.
    assert "-R" in argv


def test_a_mirror_job_still_records_what_it_is_sending(app, settings):
    """`./` is what rsync is told; the job's sources are what the app reasons about.

    They are not the same list and must not be conflated: `_estimate` prices these and
    `_update_manifest` records them, so collapsing them to `./` would leave a replicate
    updating no manifest at all and PRESENT ON permanently blank.
    """
    sources = mirror_sources(settings)
    assert ".data" in sources
    assert "urantia-library" in sources
    assert "Science" in sources and "Fiction" in sources
    # Files as well as directories: full_sync_sources filters to dirs, a verbatim copy
    # cannot.
    assert "CLAUDE.md" in sources


def test_a_mirror_push_prunes_and_a_books_push_never_does(app, settings):
    """--delete is the whole difference between Replicate and Full Sync."""
    lib = app.state.lib
    assert "--delete" in _mirror_argv(app, settings)
    books = build_argv(
        _device(app, "kobo"), lib.devices.config, full_sync_sources(settings), settings
    )
    assert "--delete" not in books


def test_a_mirror_dry_run_still_shows_the_prune(app, settings):
    """The preview is the only place you can read what --delete would remove.

    Dropping it under -n would make the dry run describe a different command than the one
    the button next to it runs.
    """
    argv = _mirror_argv(app, settings, dry_run=True)
    assert "-n" in argv and "--delete" in argv


def test_an_adopt_never_prunes(app, settings):
    """Adopt exists to change nothing but timestamps. --delete would make it a trap."""
    argv = _mirror_argv(app, settings, adopt=True)
    assert "--size-only" in argv
    assert "--delete" not in argv


def test_a_mirror_on_ext4_still_gets_its_filesystem_read_from_fs(app, settings):
    """The mode decides the shape; the filesystem still decides the flags."""
    argv = _mirror_argv(app, settings)
    assert "--no-perms" not in argv
    assert not any(a.startswith("--modify-window") for a in argv)


# ---------------------------------------------------------------- the guards --


def test_a_mirror_refuses_an_empty_source_list(app, settings):
    """--delete against no sources is the one way this feature could empty a disk."""
    lib = app.state.lib
    with pytest.raises(ValueError, match="no sources"):
        build_argv(_device(app, "thinkpad"), lib.devices.config, [], settings)


def test_a_books_push_tolerates_an_empty_source_list(app, settings):
    """The guard belongs to --delete, not to pushes in general."""
    lib = app.state.lib
    argv = build_argv(_device(app, "kobo"), lib.devices.config, [], settings)
    assert argv[0] == "rsync"  # rsync will complain; nothing is destroyed


@pytest.mark.parametrize("target", ["/", "//", ""])
def test_a_mirror_refuses_a_root_target(app, settings, target):
    lib = app.state.lib
    device = _device(app, "thinkpad").model_copy(update={"target": target})
    with pytest.raises(ValueError, match="below the root"):
        build_argv(device, lib.devices.config, mirror_sources(settings), settings)


def test_mirror_sources_are_empty_when_the_library_is_gone(settings):
    """The OSError path. build_argv is what turns [] into a refusal."""
    bad = settings.model_copy(update={"library_root": Path("/nonexistent-library")})
    assert mirror_sources(bad) == []


# --------------------------------------------------------------- the sources --


def test_mirror_sources_carry_exactly_what_the_skiplist_hides(settings):
    """The counterpart to test_library's test_full_sync_never_includes_infrastructure.

    Same tree, same scandir, opposite answer — and both are correct, which is why the mode
    exists rather than a change to SKIP_TOPLEVEL.
    """
    reader = set(full_sync_sources(settings))
    replica = set(mirror_sources(settings))
    assert reader < replica
    for hidden in ("urantia-library", ".data", "CLAUDE.md"):
        assert hidden not in reader
        assert hidden in replica


def test_the_vault_is_still_hidden_from_browsing(app):
    """Mirror-pushable is not the same as browsable, and must not become it.

    SKIP_TOPLEVEL stayed a security boundary: the index walk is untouched, so nothing here
    is reachable by browsing, searching or selecting — only by a node that names the mode.
    """
    from libnodes.library import PathError

    index = app.state.lib.index
    assert index.entry("urantia-library") is None
    assert index.entry(".data") is None
    with pytest.raises(PathError):
        index.require("urantia-library")


# ------------------------------------------------------------ the read-back --


def test_a_reader_scan_still_drops_symlinks(app, settings):
    """Without a link target in the listing, a link row is a book we cannot identify."""
    line = "lrwxrwxrwx  63 2026/08/11 12:03:46 Science/Book.pdf"
    assert parse_line(line) is None
    assert "-l" not in scan_argv(_device(app, "kobo"), settings)


def test_only_a_mirror_scan_asks_rsync_for_link_targets(app, settings):
    """-r --list-only lists a symlink but prints no `-> target`; -l is what adds it.

    Verified against rsync 3.4.1. Without this flag the blob below is unrecoverable and a
    mirror scan would report a full library as an empty one.
    """
    argv = scan_argv(_device(app, "thinkpad"), settings)
    assert "-l" in argv
    assert "--list-only" in argv


def test_a_scanned_mirror_recovers_the_blob_from_the_link_target(app):
    """The mirror scan is the *stronger* claim, not a further concession.

    An ordinary listing gives size and mtime, so `_compare` falls back to size. A link
    names its blob, and a blob match in a content-addressed library is content identity —
    which is the branch `_compare` takes first.
    """
    entry = app.state.lib.index.entry("Science/Physics/Feynman.djvu")
    assert entry is not None and entry.blob

    line = (
        f"lrwxrwxrwx  63 2026/08/11 12:03:46 "
        f"Science/Physics/Feynman.djvu -> ../../.data/{entry.blob}"
    )
    got = parse_line(line, keep_links=True)
    assert got is not None
    path, blob, size, _mtime, is_dir = got
    assert path == "Science/Physics/Feynman.djvu"
    assert blob == entry.blob
    assert is_dir is False
    # The 63 bytes of the link would be a lie about the book; the blob already says what
    # it is, so nothing needs the number.
    assert size == 0

    # And that is what makes the row an exact match rather than a stale one.
    assert _compare(entry, {"blob": blob, "size": size}) == "ok"
    assert _compare(entry, {"blob": "0" * 64, "size": size}) == "stale"


def test_a_link_to_something_that_is_not_a_blob_reports_no_blob(app):
    """A mirror may hold links out of the vault; we say nothing rather than guess."""
    line = "lrwxrwxrwx  9 2026/08/11 12:03:46 Science/odd.pdf -> /etc/hosts"
    got = parse_line(line, keep_links=True)
    assert got is not None
    assert got[0] == "Science/odd.pdf"
    assert got[1] is None


def test_a_link_whose_name_contains_the_separator_survives(app):
    """rsync's separator is the last ` -> `, not the first."""
    line = (
        "lrwxrwxrwx  63 2026/08/11 12:03:46 "
        "Science/A -> B.pdf -> ../.data/" + "a" * 64
    )
    got = parse_line(line, keep_links=True)
    assert got is not None
    assert got[0] == "Science/A -> B.pdf"
    assert got[1] == "a" * 64


def test_a_mirror_listing_counts_its_links_as_files(app):
    """parse_listing's rows are what replace_scan writes; a link must arrive as a file."""
    listing = [
        "drwxr-x---  4,096 2026/08/11 08:32:42 Science",
        "lrwxrwxrwx  63 2026/08/11 12:03:46 Science/Book.pdf -> ../.data/" + "b" * 64,
    ]
    rows = list(parse_listing(listing, keep_links=True))
    assert len(rows) == 2
    dirs = [r for r in rows if r[4]]
    files = [r for r in rows if not r[4]]
    assert [d[0] for d in dirs] == ["Science"]
    assert [f[0] for f in files] == ["Science/Book.pdf"]
    assert files[0][1] == "b" * 64

    # Same listing, reader semantics: the link is not ours to interpret.
    assert len(list(parse_listing(listing))) == 1


def test_a_mirrors_vault_is_not_reported_as_orphans(app):
    """"What is on it that we don't have" inverts on a mirror unless told the mode.

    The vault and urantia-library are on the device because Replicate put them there, and
    neither is in the index — so a plain set difference calls a correct replica ~24.6k
    files of junk to delete.
    """
    from libnodes.config import SKIP_TOPLEVEL

    lib = app.state.lib
    rows = [
        (".data/" + "c" * 64, None, 13, 1, 0),
        ("urantia-library/secrets.env", None, 13, 1, 0),
        ("Orphan.pdf", None, 99, 1, 0),
    ]
    lib.manifests.replace_scan("thinkpad", rows)
    paths = lib.index.all_file_paths()

    reader_view = lib.manifests.extras("thinkpad", paths)
    assert reader_view.total == 3

    mirror_view = lib.manifests.extras(
        "thinkpad", paths, expected_toplevel=SKIP_TOPLEVEL
    )
    # Only the genuine orphan survives — the one that is not there by design.
    assert mirror_view.total == 1
    assert [r["path"] for r in mirror_view.rows] == ["Orphan.pdf"]


async def test_the_extras_dialog_uses_the_mode(client, app):
    lib = app.state.lib
    lib.manifests.replace_scan(
        "thinkpad",
        [(".data/" + "d" * 64, None, 13, 1, 0), ("Orphan.pdf", None, 99, 1, 0)],
    )
    r = await client.get("/device/thinkpad/extras")
    assert "Orphan.pdf" in r.text
    assert "d" * 64 not in r.text


# ------------------------------------------------------------- the estimate --


def test_the_vault_is_counted_once_however_many_links_point_at_it(index):
    """DISTINCT is the point: two paths sharing a blob are one file in the vault."""
    files, size = index.vault_totals()
    assert files > 0 and size > 0
    # The fixture's five books are five distinct blobs; the dangling link contributes
    # nothing because it never became an entry.
    assert files == 5


def test_a_mirror_estimate_counts_the_vault_and_a_books_one_does_not(app, settings):
    """rsync moves the blobs as well as the links; a bar built on half of them lies."""
    lib = app.state.lib
    vault_files, _ = lib.index.vault_totals()

    reader = lib.jobs._estimate(full_sync_sources(settings))
    replica = lib.jobs._estimate(mirror_sources(settings), mirror=True)

    assert replica[0] == reader[0] + vault_files
    # The bytes are already right: a symlink's indexed size *is* the blob it points at, so
    # adding the vault's bytes would double the transfer rather than describe it.
    assert replica[1] == reader[1]


# ---------------------------------------------------------------- the routes --


async def test_the_menu_offers_replicate_to_a_mirror_and_full_sync_to_a_reader(client):
    mirror = await client.get("/device/thinkpad/menu")
    assert "Replicate" in mirror.text
    assert "Full Sync" not in mirror.text
    assert "--delete" in mirror.text

    reader = await client.get("/device/kobo/menu")
    assert "Full Sync" in reader.text
    assert "Replicate" not in reader.text
    assert "--delete" not in reader.text


async def test_the_menu_shows_a_mirror_the_command_it_will_run(client):
    """An action whose effect you have to infer from its label is a bad action."""
    r = await client.get("/device/thinkpad/menu")
    assert "--delete" in r.text
    assert "rsync" in r.text
    # And it says in prose what the argv cannot: that .data and urantia-library ride along
    # inside `./`, and what --delete will remove.
    assert ".data" in r.text
    assert "urantia-library/" in r.text
    assert "DELETES" in r.text


async def test_a_mirror_dry_run_is_offered_before_the_prune(client):
    r = await client.get("/device/thinkpad/menu")
    assert "Dry run" in r.text
    # The order is the advice: read the preview before running the thing it previews.
    assert r.text.index("Dry run") < r.text.index("Replicate")


async def test_full_sync_is_not_a_way_into_a_mirror(client):
    """Its note promises it never deletes. Routing a mirror here would break that."""
    r = await client.post("/device/thinkpad/full-sync")
    assert r.status_code == 404


async def test_replicate_is_not_offered_to_a_reader(client):
    r = await client.post("/device/kobo/replicate")
    assert r.status_code == 404


async def test_replicate_queues_a_pruning_job(client, app):
    r = await client.post("/device/thinkpad/replicate")
    assert r.status_code == 200
    job = app.state.lib.jobs.recent()[0]
    assert job.device_id == "thinkpad"
    assert "--delete" in job.argv
    assert "-L" not in job.argv
    assert job.argv[-2] == "./"
    # The vault is in what the job is about, even though rsync is told `./`.
    assert ".data" in job.sources
    assert job.label == "(replicate · whole root)"


async def test_a_mirror_node_is_not_a_selection_target(client):
    """It takes the whole root or nothing: a subtree of links has no vault to resolve."""
    picker = await client.get("/jobs/picker?path=Science")
    assert 'value="kobo"' in picker.text
    assert 'value="thinkpad"' not in picker.text

    # The row's own → buttons, asserted on the payload they would post rather than on the
    # node's name: the name also appears in the PRESENT ON badges, which mirror nodes keep.
    rows = await client.get("/lib/list")
    assert '"device": "kobo"' in rows.text
    assert '"device": "thinkpad"' not in rows.text


async def test_a_selection_post_to_a_mirror_is_refused(client, app):
    """A hidden button is not a guard — this is a form post."""
    before = len(app.state.lib.jobs.recent())
    r = await client.post(
        "/jobs", data={"device": ["thinkpad"], "path": ["Science"], "confirmed": "yes"}
    )
    assert r.status_code == 200
    assert "Replicate" in r.text
    assert len(app.state.lib.jobs.recent()) == before


async def test_a_mirror_retry_replays_the_whole_root_not_the_browsable_part(client, app):
    """`_resolve` would strip .data/ while --delete stayed. Re-derive instead."""
    await client.post("/device/thinkpad/replicate")
    first = app.state.lib.jobs.recent()[0]

    r = await client.post(f"/jobs/{first.id}/retry")
    assert r.status_code == 200
    again = app.state.lib.jobs.recent()[0]
    assert again.id != first.id
    # Re-derived, not narrowed to what the index vouches for.
    assert ".data" in again.sources
    assert "urantia-library" in again.sources
    assert again.argv[-2] == "./"
    assert "--delete" in again.argv
    assert again.label == "(replicate · whole root)"


async def test_a_retry_repeats_a_dry_run_rather_than_committing_it(client, app):
    """On a mirror this is the difference between a preview and an irreversible prune."""
    await client.post("/device/thinkpad/dry-run")
    preview = app.state.lib.jobs.recent()[0]
    assert preview.dry_run is True

    await client.post(f"/jobs/{preview.id}/retry")
    again = app.state.lib.jobs.recent()[0]
    assert again.dry_run is True
    assert "-n" in again.argv


# --------------------------------------------------------------------- the UI --


async def test_the_row_badges_a_mirror_node(client, settings):
    rows = await client.get("/devices/rows")
    assert "MIRROR" in rows.text
    # One badge, on one node: the reader rows must not carry it.
    assert rows.text.count("MIRROR") == 1
    # The tooltip names what it replicates. An undefined name in Jinja renders empty and
    # would have left "replicates  verbatim" reading as a bug.
    assert f"replicates {settings.library_root} verbatim" in rows.text


async def test_the_card_badges_a_mirror_node(client):
    cards = await client.get("/devices/grid")
    assert cards.text.count("MIRROR") == 1


def test_the_mirror_badge_added_no_grid_column():
    """The badge sits inside the Type cell precisely so this stays true.

    A grid whose template grew a column the stylesheet does not know about still renders —
    it wraps the last cell onto a second line, silently. Guarded here as well as in
    test_battery.py because this change is what put a second element in that cell.
    """
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()
    block = css.split(".device-grid {")[1].split("}")[0]
    tracks = len(re.findall(r"minmax\(", block))

    row = (ROOT / "libnodes" / "templates" / "device_row.html").read_text()
    cells = len(re.findall(r"^  <div [^>]*data-label=", row, re.MULTILINE))

    assert tracks == cells, f"{tracks} CSS tracks against {cells} row cells"
