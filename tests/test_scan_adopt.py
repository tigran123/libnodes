"""Adopting a device that already holds the library.

A device populated by other means (a card reader, an older script) is both invisible to
LibNodes and, worse, looks entirely out of date: its files carry the mtimes of whenever
they were copied, so rsync's default size+mtime check wants to re-send all 249 GB of
them. Scan solves the first problem, adopt the second.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from libnodes.jobs import build_argv
from libnodes.scan import parse_line, parse_listing, scan_argv

# Verbatim `rsync -r --list-only` output from the LG over Termux sshd.
LISTING = """\
drwxr-x---         32,768 2026/08/11 14:22:20 .
drwxr-x---         32,768 2026/08/11 08:32:42 Art
-rwxr-x---     21,669,813 2026/08/11 08:32:40 Art/Complete-Book-of-Drawing-Techniques.pdf
-rwxr-x---      1,048,176 2026/08/11 12:03:46 Science/Chess/Averbakh-Endshpile.djvu
-rwxr-x---            512 2026/08/11 12:03:46 Science/Chess/A file with spaces.pdf
lrwxrwxrwx             12 2026/08/11 12:03:46 Science/link -> elsewhere
"""


# ------------------------------------------------------------------ parsing --


def test_parses_a_file_line():
    got = parse_line(
        "-rwxr-x---     21,669,813 2026/08/11 08:32:40 Art/Complete-Book.pdf"
    )
    assert got is not None
    path, size, mtime, is_dir = got
    assert path == "Art/Complete-Book.pdf"
    assert size == 21_669_813
    assert mtime > 0
    assert is_dir is False


def test_directories_are_kept():
    """An empty directory has no files to count; its own row is the only evidence."""
    got = parse_line("drwxr-x---  32,768 2026/08/11 08:32:42 Art")
    assert got is not None
    assert got[0] == "Art"
    assert got[3] is True


def test_skips_symlinks_and_self():
    assert parse_line("drwxr-x---  32,768 2026/08/11 14:22:20 .") is None
    assert parse_line("lrwxrwxrwx  12 2026/08/11 12:03:46 Science/link -> x") is None


def test_skips_junk():
    for junk in ("", "   ", "receiving file list ... done", "sent 20 bytes"):
        assert parse_line(junk) is None


def test_filenames_with_spaces_survive():
    rows = list(parse_listing(LISTING.splitlines()))
    paths = [r[0] for r in rows]
    assert "Science/Chess/A file with spaces.pdf" in paths


def test_listing_yields_files_and_dirs_but_never_a_blob():
    rows = list(parse_listing(LISTING.splitlines()))
    # 3 files + 1 dir ("Art"); the "." self-entry and the symlink are dropped.
    assert len(rows) == 4
    dirs = [r for r in rows if r[4]]
    files = [r for r in rows if not r[4]]
    assert [d[0] for d in dirs] == ["Art"]
    assert len(files) == 3
    for _path, blob, _size, _mtime, _is_dir in rows:
        # A remote listing cannot report content; claiming a hash would be a lie.
        assert blob is None
    assert all(f[2] > 0 for f in files)


def test_scan_argv_targets_the_device(app, settings):
    device = app.state.lib.devices.config.by_id["kobo"]
    argv = scan_argv(device, settings)
    assert argv[0] == "rsync"
    assert "--list-only" in argv
    assert argv[-1] == "root@127.0.0.1:/mnt/onboard/Books/"
    ssh = argv[argv.index("-e") + 1]
    assert "-p 2222" in ssh


# ------------------------------------------------------------------- adopt --


def test_adopt_uses_size_only(app, settings):
    """The measured fix: `<f` (transfer) becomes `.f` (skip, but repair mtime)."""
    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"]
    argv = build_argv(device, lib.devices.config, ["Science"], settings, adopt=True)
    assert "--size-only" in argv
    # FAT and Android's FUSE cannot store permissions; without this every run would
    # forever report them as differing.
    assert "--no-perms" in argv
    # Still a real sync in every other respect.
    assert "-L" in argv and "-R" in argv


def test_a_normal_push_is_not_size_only(app, settings):
    """--size-only is a deliberate, narrow concession — not the default."""
    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"]
    argv = build_argv(device, lib.devices.config, ["Science"], settings)
    assert "--size-only" not in argv


async def test_adopt_job_is_recorded_as_such(client, app):
    r = await client.post("/device/kobo/adopt")
    assert r.status_code == 200
    job = app.state.lib.jobs.recent()[0]
    assert job.adopt is True
    assert "--size-only" in job.argv
    assert job.label == "(adopt existing copy)"


async def test_adopt_survives_a_restart_as_an_adopt_job(settings, app, client):
    from libnodes.jobs import JobStore

    await client.post("/device/kobo/adopt")
    job_id = app.state.lib.jobs.recent()[0].id
    assert JobStore(settings.jobs_db).get(job_id).adopt is True


# -------------------------------------------------------------- staleness --


def test_scanned_rows_are_not_stale_merely_for_having_old_mtimes(settings, index):
    """The bug this pins: mtime comparison marked a correct 249 GB library stale.

    A scan reports the device's own timestamps, which are whenever the files were
    copied there. Size is the only honest signal we have.
    """
    from libnodes.manifests import Manifests

    manifests = Manifests(settings.manifests_db)
    entry = index.entry("Fiction/Joyce/Ulysses.pdf")

    # Correct content, wildly different mtime — exactly the LG's situation.
    manifests.replace_scan("kobo", [(entry.path, None, entry.size, entry.mtime + 9_000_000)])

    states = manifests.presence([entry], ["kobo"])
    assert states[entry.path][0].presence == "ok"


def test_scanned_rows_are_stale_when_the_size_differs(settings, index):
    from libnodes.manifests import Manifests

    manifests = Manifests(settings.manifests_db)
    entry = index.entry("Fiction/Joyce/Ulysses.pdf")
    manifests.replace_scan("kobo", [(entry.path, None, entry.size + 1024, entry.mtime)])
    assert manifests.presence([entry], ["kobo"])[entry.path][0].presence == "stale"


def test_a_pushed_row_still_uses_the_exact_hash(settings, index):
    """Where we do know the content, we use it — scanning must not weaken that."""
    from libnodes.manifests import Manifests

    manifests = Manifests(settings.manifests_db)
    entry = index.entry("Fiction/Joyce/Ulysses.pdf")
    manifests.record("kobo", [(entry.path, "f" * 128, entry.size, entry.mtime)])
    assert manifests.presence([entry], ["kobo"])[entry.path][0].presence == "stale"


def test_an_empty_directory_on_the_device_reads_as_present(settings, index, library):
    """The regression: /Books/Unsorted is on both sides but showed `—`.

    It holds no files, so the file-count evidence is zero either way; only the
    directory's own manifest row can distinguish "there and empty" from "never sent".
    """
    from libnodes.library import LibraryIndex
    from libnodes.manifests import Manifests

    (library / "Unsorted").mkdir()
    ix = LibraryIndex(settings)
    ix.reindex()
    entry = ix.entry("Unsorted")
    assert entry is not None and entry.is_dir and (entry.files or 0) == 0

    manifests = Manifests(settings.manifests_db)
    manifests.replace_scan("kobo", [("Unsorted", None, 0, 0, 1)])

    states = manifests.presence([entry], ["kobo"])["Unsorted"]
    assert [s.presence for s in states] == ["ok"]
    assert states[0].detail == "empty"


def test_an_empty_directory_absent_from_the_device_stays_absent(settings, index, library):
    """The other half: no directory row means we genuinely do not know it is there."""
    from libnodes.library import LibraryIndex
    from libnodes.manifests import Manifests

    (library / "Unsorted").mkdir()
    ix = LibraryIndex(settings)
    ix.reindex()
    entry = ix.entry("Unsorted")

    manifests = Manifests(settings.manifests_db)
    manifests.replace_scan("kobo", [("Fiction/other.epub", None, 10, 0, 0)])
    assert manifests.presence([entry], ["kobo"])["Unsorted"] == []


def test_directory_rows_do_not_inflate_the_file_count(settings, index):
    """The device reports 20,782 files and 3,839 directories; only files are files."""
    from libnodes.manifests import Manifests

    manifests = Manifests(settings.manifests_db)
    manifests.replace_scan(
        "kobo",
        [
            ("Fiction", None, 0, 0, 1),
            ("Fiction/a.epub", None, 100, 0, 0),
            ("Fiction/b.epub", None, 200, 0, 0),
        ],
    )
    files, total_bytes, _ = manifests.summary("kobo")
    assert files == 2
    assert total_bytes == 300


def test_a_pushed_empty_directory_is_recorded_too(app, tmp_path, monkeypatch, library):
    """rsync -R creates the directory, so the manifest should say so."""
    (library / "Unsorted").mkdir()
    lib = app.state.lib
    lib.index.reindex()

    job_sources = ["Unsorted"]
    recorded: list[tuple] = []
    monkeypatch.setattr(
        lib.manifests, "record", lambda dev, rows, source="push": recorded.extend(rows)
    )

    from libnodes.jobs import Job

    lib.jobs._update_manifest(
        Job(id=1, device_id="kobo", sources=job_sources, label="x")
    )
    assert ("Unsorted", None, 0, recorded[0][3], 1) == recorded[0]


async def test_scan_replaces_rather_than_accumulates(settings, index):
    """A file deleted on the device must leave the manifest."""
    from libnodes.manifests import Manifests

    manifests = Manifests(settings.manifests_db)
    manifests.replace_scan("kobo", [("a.pdf", None, 1, 0), ("b.pdf", None, 2, 0)])
    assert manifests.summary("kobo")[0] == 2
    manifests.replace_scan("kobo", [("a.pdf", None, 1, 0)])
    assert [r.path for r in manifests.rows_for("kobo")] == ["a.pdf"]


# ------------------------------------------------------------- mangled names --


def test_demangle_recovers_double_encoded_utf8():
    """A real device held `01 ÐÑÐ·ÑÐºÐ°.flac` — UTF-8 bytes re-encoded as Latin-1."""
    from libnodes.scan import demangle

    mangled = "Музыка.flac".encode("utf-8").decode("latin-1")
    assert demangle(mangled) == "Музыка.flac"


def test_demangle_leaves_correct_names_alone():
    from libnodes.scan import demangle

    assert demangle("Science/Chess/Tal.pdf") is None
    assert demangle("Музыка.flac") is None  # already correct


def test_demangle_never_raises_on_junk():
    from libnodes.scan import demangle

    for junk in ("", "日本語.pdf", "\udcff broken surrogate", "a" * 300):
        demangle(junk)  # must not raise


def test_extras_flags_mangled_duplicates(settings, index):
    """The payoff: a wall of gibberish becomes 'duplicate of X, safe to delete'."""
    from libnodes.manifests import Manifests

    manifests = Manifests(settings.manifests_db)
    real = "Fiction/Joyce/Ulysses.pdf"
    mangled = real.encode("utf-8").decode("latin-1")  # identical here (ASCII)

    cyrillic_real = "Fiction/Музыка.pdf"
    cyrillic_mangled = cyrillic_real.encode("utf-8").decode("latin-1")

    manifests.replace_scan(
        "kobo",
        [
            (cyrillic_mangled, None, 100, 0, 0),
            ("Fiction/genuinely-orphaned.pdf", None, 200, 0, 0),
        ],
    )

    library = {cyrillic_real, real}
    found = manifests.extras("kobo", library)

    assert found.known is True
    assert found.total == 2
    assert found.duplicates == 1
    assert found.listed_bytes == 300
    by_path = {r["path"]: r for r in found.rows}
    assert by_path[cyrillic_mangled]["real"] == cyrillic_real
    assert by_path[cyrillic_mangled]["duplicate"] is True
    orphan = by_path["Fiction/genuinely-orphaned.pdf"]
    assert orphan["duplicate"] is False


def test_extras_is_empty_when_the_device_matches(settings, index):
    from libnodes.manifests import Manifests

    manifests = Manifests(settings.manifests_db)
    entry = index.entry("Fiction/Joyce/Ulysses.pdf")
    manifests.replace_scan("kobo", [(entry.path, None, entry.size, entry.mtime, 0)])
    found = manifests.extras("kobo", {entry.path})
    assert (found.rows, found.total, found.duplicates) == ([], 0, 0)
    # ...and it is empty because a scan looked, which is the whole difference.
    assert found.known is True
    assert found.scanned_at is not None


def test_extras_says_it_does_not_know_when_the_device_was_never_scanned(settings, index):
    """BK502: 0 manifest rows, 36 files on the card, "nothing extra" on the screen.

    An empty set difference is not an answer, and a device nobody has ever listed
    produces exactly the same empty list as a device that genuinely holds nothing extra.
    """
    from libnodes.manifests import Manifests

    manifests = Manifests(settings.manifests_db)
    found = manifests.extras("bk", index.all_file_paths())

    assert found.known is False
    assert found.scanned_at is None
    assert (found.rows, found.total) == ([], 0)


def test_a_push_manifest_cannot_answer_what_is_on_the_device(settings, index):
    """note10: 20,782 push rows, no scan, and a clean bill of health it never checked.

    A push manifest records what LibNodes sent, so it can never contain a file LibNodes
    did not send. Subtracting the library from it is guaranteed to yield nothing.
    """
    from libnodes.manifests import Manifests

    manifests = Manifests(settings.manifests_db)
    entry = index.entry("Fiction/Joyce/Ulysses.pdf")
    manifests.record("kobo", [(entry.path, entry.blob, entry.size, entry.mtime, 0)])
    # A stale push row for a book since deleted from the library: still not evidence of
    # what is on the card, because it is evidence of what we sent.
    manifests.record("kobo", [("Fiction/deleted-since.pdf", None, 10, 0, 0)])

    library = {entry.path}
    assert manifests.extras("kobo", library).known is False

    # One scan, and the same question becomes answerable — over what the scan saw.
    manifests.replace_scan("kobo", [("MoonReader/settings.db", None, 4096, 0, 0)])
    found = manifests.extras("kobo", library)
    assert found.known is True
    assert [r["path"] for r in found.rows] == ["MoonReader/settings.db"]


# ------------------------------------------------- staleness is visible --


async def test_present_on_chips_carry_their_own_provenance(client, app, index):
    """Where the freshness lives now: on each chip, not in a device-wide rail figure.

    A per-row tooltip can say when THAT file was last seen; a summary in the rail
    cannot, and went stale the moment anything else changed.
    """
    lib = app.state.lib
    entry = index.entry("Science/Physics/Landau.pdf")
    lib.manifests.record_entries("kobo", [entry], source="scan")

    r = await client.get("/lib/list", params={"p": "Science/Physics"})
    assert "seen in scan" in r.text
    assert "Test Kobo ·" in r.text


async def test_manifest_keeps_stale_rows_until_a_rescan(settings, index, app, client):
    """Deleting files ON the device cannot reach us; only a rescan corrects it.

    This is the behaviour the user hit: a directory removed by hand still showed as
    present, because the last thing we knew said it was.
    """
    lib = app.state.lib
    entries = [
        index.entry("Science/Physics/Landau.pdf"),
        index.entry("Science/Physics/Feynman.djvu"),
    ]
    lib.manifests.record_entries("kobo", entries, source="scan")
    assert len(lib.manifests.presence(entries, ["kobo"])[entries[0].path]) == 1

    # The device loses one file behind our back; the manifest still claims both.
    assert lib.manifests.summary("kobo")[0] == 2

    # A rescan is authoritative and drops what is no longer there.
    lib.manifests.replace_scan(
        "kobo", [(entries[0].path, None, entries[0].size, entries[0].mtime, 0)]
    )
    assert lib.manifests.summary("kobo")[0] == 1
    assert lib.manifests.presence([entries[1]], ["kobo"])[entries[1].path] == []


# --------------------------------------------------- actions are legible --


async def test_actions_menu_shows_the_command_each_action_runs(client):
    """An action you have to guess at is a bad action.

    "Adopt existing copy" means nothing until you can see the --size-only that makes it
    safe, so every action states the command it will invoke.
    """
    r = await client.get("/device/kobo/menu")
    assert r.status_code == 200

    # The adopt command, verbatim enough to read its intent.
    assert "--size-only" in r.text
    assert "--no-perms" in r.text
    # The scan command.
    assert "--list-only" in r.text
    # And -L, the flag that makes a CAS library transfer real files.
    assert "-L" in r.text
    # Test is not in this dialog any more; it is a button on the row.
    assert "df -Pk" not in r.text


async def test_actions_menu_commands_are_copyable(client):
    r = await client.get("/device/kobo/menu")
    assert "data-copy=" in r.text


async def test_the_extras_dialog_never_claims_a_device_it_has_not_seen(client, app, index):
    """The screen that started this: a positive claim about a device never looked at.

    Device-scoped fragments cannot be parametrised into FRAGMENTS in test_routes.py, so
    the HTMX contract is asserted here too.
    """
    r = await client.get("/device/kobo/extras")
    assert r.status_code == 200
    assert "<html" not in r.text.lower()

    assert "never scanned" in r.text
    assert "exactly what the library has" not in r.text
    # ...and the scan is offered where the question was asked, command and all.
    assert "/device/kobo/scan" in r.text
    assert "--list-only" in r.text

    # Once a scan has looked, the reassurance is allowed — and dated.
    entry = index.entry("Fiction/Joyce/Ulysses.pdf")
    app.state.lib.manifests.replace_scan(
        "kobo", [(entry.path, None, entry.size, entry.mtime, 0)]
    )
    r = await client.get("/device/kobo/extras")
    assert "exactly what the library has" in r.text
    assert "as the scan" in r.text


async def test_the_extras_dialog_finishes_the_scan_it_started(client, app):
    """Offering the scan is only half of it: the dialog has to come back with the answer.

    The scan POST replies with a status line, not a dialog, so the button re-requests
    this route and the reply — rendered while the scan is running — carries the poll that
    turns itself into the list. If either half is missing the user presses Scan and the
    dialog sits on "never scanned" for ever.
    """
    idle = (await client.get("/device/kobo/extras")).text
    assert 'hx-trigger="every 3s"' not in idle             # nothing to wait for yet
    # ...but the button re-requests this dialog once the scan is under way. The JS is
    # HTML-escaped in the attribute, hence &#39; where the source has a quote.
    assert "hx-on::after-request=" in idle
    assert "htmx.ajax(&#39;GET&#39;,&#39;/device/kobo/extras&#39;" in idle

    app.state.lib.scanner._running.add("kobo")
    running = (await client.get("/device/kobo/extras")).text
    assert 'hx-get="/device/kobo/extras"' in running
    assert 'hx-trigger="every 3s"' in running
    assert 'hx-swap="outerHTML"' in running
    assert "scanning" in running


async def test_full_sync_lives_in_the_actions_menu(client):
    """It used to be a separate row button; one entry point is easier to reason about."""
    menu = await client.get("/device/kobo/menu")   # fixture kobo has full_sync: true
    assert "Full Sync" in menu.text
    assert "/device/kobo/full-sync" in menu.text

    rows = await client.get("/devices/rows")
    assert "Full Sync" not in rows.text
    assert "Actions" in rows.text


async def test_device_rows_no_longer_shortcut_to_the_library(client):
    """The Push… button was only ever a link to /library."""
    rows = await client.get("/devices/rows")
    assert "Push…" not in rows.text
    assert "/library?target=" not in rows.text


async def test_a_device_without_full_sync_is_not_offered_it(client, devices_file):
    """full_sync is a capability flag, not decoration."""
    r = await client.get("/device/phone/menu")     # fixture phone has no full_sync
    assert "Full Sync" not in r.text
    assert "Scan device" in r.text


async def test_log_button_says_see_not_save(client, app, tmp_path, monkeypatch):
    """Logs are written to disk regardless; the button opens one."""
    fake = tmp_path / "r"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    async with app.router.lifespan_context(app):
        monkeypatch.setattr("libnodes.jobs.build_argv", lambda *a, **k: [str(fake)])
        lib = app.state.lib
        job = lib.jobs.submit(lib.devices.config.by_id["kobo"], ["Fiction"])
        for _ in range(60):
            if lib.jobs.get(job.id).finished:
                break
            await asyncio.sleep(0.05)

        r = await client.get("/jobs/dock")
        assert "See log" in r.text
        assert "Save log" not in r.text


# ------------------------------------------------- actions give feedback --


async def test_every_action_reports_back_somewhere(client):
    """The bug: job-creating actions toasted into #notices, which sits BELOW the
    dialog backdrop (z-index 50 vs 60) — so the toast was invisible and the button
    looked dead. Those actions now close the dialog first."""
    r = await client.get("/device/kobo/menu")

    import re

    blocks = r.text.split('<div class="action">')[1:]
    assert len(blocks) >= 4
    for block in blocks:
        target = re.search(r'hx-target="([^"]+)"', block).group(1)
        closes = "device-menu')?.remove()" in block
        # Either it lands somewhere inside the dialog, or it closes the dialog so the
        # toast is visible. Never neither.
        assert target.startswith("#scan-status") or target.startswith("#test-result") or closes, block[:200]


async def test_every_action_points_at_a_real_url(client, app):
    """The bug this pins: a dict key named `get` resolves to dict.get in Jinja, so the
    button rendered hx-get="<built-in method get of dict object>" and did nothing."""
    import re

    r = await client.get("/device/kobo/menu")
    actions = re.findall(r'hx-(get|post)="([^"]+)"', r.text)
    assert actions, "no actions rendered"

    for method, url in actions:
        assert url.startswith("/device/kobo/"), url
        assert "built-in" not in url and "object at" not in url, url
        # The only assertion that would actually have caught the bug: call it.
        got = await (
            client.get(url) if method == "get" else client.post(url)
        )
        assert got.status_code != 404, f"{method} {url} -> 404"


async def test_closing_the_dialog_does_not_cancel_the_request(client):
    """htmx abandons a request whose triggering element leaves the DOM, so the dialog
    must close after the response, not on click."""
    r = await client.get("/device/kobo/menu")
    assert "onclick=\"document.getElementById('device-menu').remove()\"" not in r.text.replace(
        '<button class="btn" type="button"\n              onclick="document.getElementById(\'device-menu\').remove()">Close</button>', ""
    )
    assert "hx-on::after-request" in r.text


async def test_test_is_a_row_button_carrying_the_command_it_runs(client):
    """The one action that writes nothing gets to live in the row.

    It is also the action you most want when a device is offline — which is exactly when
    the Actions dialog is not offered. With no command strip out there, the tooltip keeps
    the house rule, and it must carry the command that actually runs: the dialog used to
    advertise a bare `df -Pk <target>` while the handler ran a three-part script.

    Asserted on the rendered row rather than by pressing it, so this costs no ssh.
    """
    rows = await client.get("/devices/rows")
    assert 'hx-post="/device/kobo/test"' in rows.text
    assert ">Test<" in rows.text
    assert "Test connection" not in rows.text
    assert "# rsync" in rows.text          # from _TEST_SCRIPT, via the tooltip
    assert "test -w" in rows.text

    menu = await client.get("/device/kobo/menu")
    assert "/device/kobo/test" not in menu.text
    assert "Test connection" not in menu.text


async def test_connection_test_reports_failure_legibly(client, app):
    """The fixture device is unreachable, so this exercises the failure path.

    The only test that actually runs the probe, so the result's shape is asserted here
    too rather than in a test of its own that would spawn a second ssh for nothing.
    """
    r = await client.post("/device/kobo/test")
    assert r.status_code == 200
    assert "Connection test" in r.text
    assert "is-err" in r.text
    assert "$ ssh" in r.text          # the command is echoed
    assert "hint:" in r.text          # ...and a likely cause named
    assert "# rsync" in r.text        # the echoed line is the one that ran, in full

    # It arrives as a dialog of its own, dismissable, rather than as a strip that only
    # made sense inside the Actions menu it no longer lives in.
    assert 'id="test-result"' in r.text
    assert "getElementById('test-result').remove()" in r.text

    # The handler re-probes on the way here, so the row rides along out of band.
    assert 'hx-swap-oob="true"' in r.text
    assert 'id="node-kobo"' in r.text


async def test_the_test_updates_the_storage_it_just_measured(client, app, monkeypatch):
    """The row used to ride along out of band carrying the *previous* poll's storage.

    `probe()` on the way out refreshes reachability only, so the dialog printed the df it
    had just run while the STORAGE cell behind it showed a figure up to freespace_interval
    (5 min) old — the two disagreeing on screen at the same moment. The reading is already
    paid for; the fix is to keep it, not to spend a second ssh.
    """
    from libnodes.probe import FreeSpace, Reachability

    lib = app.state.lib
    now = time.time()
    lib.probe._slot("kobo").reach = Reachability(
        state="online", last_ok=now, checked_at=now
    )
    # What the row is showing: 5 GB used of 30.
    lib.probe._slot("kobo").space = FreeSpace(
        total=32212254720, used=5368709120, free=26843545600, checked_at=now - 600
    )

    # What the test measures: 10 GB used. 1K blocks, as `df -Pk` reports them.
    transcript = (
        "# df\n"
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/mmcblk0p3 31457280 10485760 20971520 33% /mnt/onboard\n"
        "# rsync\nrsync  version 3.2.7  protocol version 31\n"
        "# write\nwritable\n"
    )

    class _Proc:
        returncode = 0

        async def communicate(self):
            return (transcript.encode(), b"")

    async def fake_exec(*a, **k):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    r = await client.post("/device/kobo/test")
    assert r.status_code == 200

    space = lib.probe.space("kobo")
    assert space.used == 10737418240, "the test threw away the df it had just run"
    assert space.total == 32212254720
    assert space.free == 21474836480
    # ...and the row it swaps out of band says so, rather than the old figure.
    assert "10.0 GB used" in r.text
    assert "5.0 GB used" not in r.text


async def test_the_rsync_banner_is_not_read_as_a_df_record(app):
    """`_parse_df` scans backwards for the last line that parses, and
    `rsync  version 3.2.7  protocol version 31` splits into the six fields it wants.
    Handing it the whole transcript would let the version line answer for the disk."""
    from libnodes.routes.devices import _df_section

    transcript = (
        "# df\n"
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/mmcblk0p3 31457280 10485760 20971520 33% /mnt/onboard\n"
        "# rsync\nrsync  version 3.2.7  protocol version 31\n"
        "# write\nwritable\n"
    )
    section = _df_section(transcript)
    assert "rsync" not in section
    assert "writable" not in section
    assert "31457280" in section

    lib = app.state.lib
    assert lib.probe.adopt_space("kobo", section) is True
    assert lib.probe.space("kobo").used == 10737418240

    # A device that answered but whose df said nothing usable leaves the reading alone
    # rather than blanking a figure the last real probe established.
    before = lib.probe.space("kobo")
    assert lib.probe.adopt_space("kobo", "# df\ndf: /nope: No such file\n") is False
    assert lib.probe.space("kobo") is before


async def test_a_dry_run_does_not_claim_to_have_updated_the_manifest(
    app, fake_rsync, monkeypatch
):
    """The sign-off line sat outside the `if not job.dry_run` guard, so the one job that
    cannot change a device finished by announcing "manifest updated · index re-scanned".
    Neither half was true: nothing was recorded, and nothing re-scans the index at all —
    which is exactly the line a user reads before asking why PRESENT ON has not moved."""
    lib = app.state.lib
    async with app.router.lifespan_context(app):
        monkeypatch.setattr(
            "libnodes.jobs.build_argv", lambda *a, **k: [str(fake_rsync)]
        )
        device = lib.devices.config.by_id["kobo"]
        job = lib.jobs.submit(device, ["Fiction"], dry_run=True)
        for _ in range(100):
            if lib.jobs.get(job.id).finished:
                break
            await asyncio.sleep(0.05)

        assert lib.jobs.get(job.id).state == "done"
        text = "\n".join(line for _css, line in lib.jobs.terminal(job.id))
        assert "manifest updated" not in text
        assert "index re-scanned" not in text
        assert "manifest unchanged" in text
        # ...and the claim it declined to make is the true one: nothing was recorded.
        assert lib.manifests.summary("kobo")[0] == 0


async def test_no_action_is_styled_as_primary(client):
    """Full Sync moves 250 GB; it should not be the most inviting button on screen."""
    r = await client.get("/device/kobo/menu")
    body = r.text.split('<div class="dialog-body">')[1]
    assert "btn-primary" not in body


async def test_the_manifest_only_action_says_it_runs_nothing(client):
    r = await client.get("/device/kobo/menu")
    assert "no command" in r.text
    assert "Touches no device" in r.text


# ------------------------------------------------------------- dry run --


async def test_dry_run_action_is_offered_beside_full_sync(client):
    """On a device that already holds the library, 'would anything move?' is the
    question you want answered before committing to a 250 GB push."""
    r = await client.get("/device/kobo/menu")
    assert "Dry run" in r.text
    assert "/device/kobo/dry-run" in r.text
    # ...and it shows the -n command, so it is obviously harmless.
    assert " -n " in r.text


async def test_dry_run_queues_a_real_job_that_changes_nothing(client, app):
    r = await client.post("/device/kobo/dry-run")
    assert r.status_code == 200
    job = app.state.lib.jobs.recent()[0]
    assert job.dry_run is True
    assert "-n" in job.argv
    assert job.label == "(dry run · full library)"
    # No --delete, and nothing that could write.
    assert "--delete" not in job.argv


async def test_dry_run_never_touches_the_manifest(app, tmp_path, monkeypatch):
    """A preview that recorded what it previewed would be worse than useless."""
    fake = tmp_path / "r"
    fake.write_text("#!/bin/sh\necho 'sending incremental file list'\nexit 0\n")
    fake.chmod(0o755)

    async with app.router.lifespan_context(app):
        monkeypatch.setattr("libnodes.jobs.build_argv", lambda *a, **k: [str(fake)])
        lib = app.state.lib
        job = lib.jobs.submit(
            lib.devices.config.by_id["kobo"], ["Fiction"], dry_run=True
        )
        for _ in range(80):
            if lib.jobs.get(job.id).finished:
                break
            await asyncio.sleep(0.05)

        assert lib.jobs.get(job.id).state == "done"
        assert lib.manifests.summary("kobo")[0] == 0, "a dry run wrote to the manifest"


async def test_dry_run_is_labelled_in_history(client, app):
    await client.post("/device/kobo/dry-run")
    rows = await client.get("/jobs/rows")
    assert "DRY RUN" in rows.text


def test_adopt_does_not_force_no_perms_on_a_real_filesystem(app, settings):
    """--no-perms belongs to the filesystem decision, not to adopt.

    Forcing it here weakened an adopt onto ext4, where permissions are real and are
    exactly the kind of metadata an adoption run should be repairing.
    """
    from libnodes.models import parse_devices
    from libnodes.jobs import build_argv

    cfg, _ = parse_devices(
        "devices:\n  - id: fat\n    name: F\n    type: termux\n    host: h\n"
        "    target: /sd\n    fs: vfat\n"
        "  - id: ext\n    name: E\n    type: linux\n    host: h\n"
        "    target: /srv\n    fs: ext4\n"
    )
    fat = build_argv(cfg.by_id["fat"], cfg, ["S"], settings, adopt=True)
    ext = build_argv(cfg.by_id["ext"], cfg, ["S"], settings, adopt=True)

    assert "--size-only" in fat and "--size-only" in ext
    assert "--no-perms" in fat
    assert "--no-perms" not in ext


def test_fat_gets_a_modify_window_and_ext4_does_not(settings):
    """FAT stores the seconds field in units of two.

    A timestamp rsync writes reads back up to a second earlier, rsync compares exactly,
    and the file is re-sent — for ever. Measured against a real FAT32 SD card: 8,786 of
    24,620 files wanted re-sending on every push, 0 with the window. It is per-filesystem
    because on ext4 the timestamps are exact, and an exact comparison is what notices a
    book edited in place.
    """
    from libnodes.models import parse_devices
    from libnodes.jobs import build_argv

    cfg, _ = parse_devices(
        "devices:\n  - id: fat\n    name: F\n    type: termux\n    host: h\n"
        "    target: /sd\n    fs: vfat\n"
        "  - id: ext\n    name: E\n    type: linux\n    host: h\n"
        "    target: /srv\n    fs: ext4\n"
    )
    fat = build_argv(cfg.by_id["fat"], cfg, ["S"], settings)
    ext = build_argv(cfg.by_id["ext"], cfg, ["S"], settings)

    assert "--modify-window=1" in fat
    assert not any(a.startswith("--modify-window") for a in ext)


def test_a_termux_device_without_fs_still_gets_the_window(settings):
    """`fs:` is optional in the schema, so the fallback has to carry the fix too.

    The real fleet does set it — all three devices say `fs: vfat` — but `Device.fs` is
    `None`-able and `effective_fs` (`models.py`) then guesses vfat for anything that is
    not `type: linux`. A device that inherits the guess is on the same FAT card as one
    that declares it, and must get the same window.
    """
    from libnodes.models import parse_devices
    from libnodes.jobs import build_argv

    cfg, _ = parse_devices(
        "devices:\n  - id: lg\n    name: LG\n    type: termux\n    host: h\n"
        "    target: /sdcard/Books\n"
    )
    argv = build_argv(cfg.by_id["lg"], cfg, ["Audio"], settings)
    assert "--modify-window=1" in argv
