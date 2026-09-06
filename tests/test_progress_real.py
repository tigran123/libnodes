"""Regression tests against real rsync output captured from a real transfer.

`tests/data_rsync_human.log` is verbatim stdout from pushing Science/Chess (14 files,
49 MB) to a real host with the default `-avhP --partial --info=progress2` flags. It
exists because a hand-written fixture used rsync's *plain* byte column, while `-h` —
which is in the default flags — produces a human-readable one, and the parser silently
reported 0 bytes for every genuine transfer.
"""

from __future__ import annotations

import asyncio

from pathlib import Path

import pytest

from libnodes.jobs import PROGRESS_RE, Job, _apply_progress, parse_size_token

LOG = (Path(__file__).parent / "data_rsync_human.log").read_text().splitlines()


@pytest.mark.parametrize(
    "token,expected",
    [
        ("734.38K", int(734.38 * 1024)),
        ("1.78M", int(1.78 * 1024**2)),
        ("51.61M", int(51.61 * 1024**2)),
        ("1,234,567", 1234567),
        ("0", 0),
        ("2.5G", int(2.5 * 1024**3)),
    ],
)
def test_parse_size_token(token, expected):
    assert parse_size_token(token) == expected


def test_human_readable_progress_lines_are_matched():
    """The bug: -h formats bytes as 734.38K and the old regex only took digits."""
    matched = [ln for ln in LOG if PROGRESS_RE.match(ln)]
    # 16 progress lines in this capture; the point is that the human-formatted ones
    # (every line but the initial `0`) are among them.
    assert len(matched) >= 15
    assert any("734.38K" in ln for ln in matched)
    assert any("51.61M" in ln for ln in matched)


def test_final_progress_line_gives_real_totals():
    job = Job(id=1, device_id="d", sources=[], label="x")
    for line in LOG:
        m = PROGRESS_RE.match(line)
        if m:
            _apply_progress(job, m)

    assert job.pct == 100
    assert job.bytes_done == parse_size_token("51.61M")
    assert job.bytes_done > 50_000_000  # not 0, which is what the bug produced
    assert job.entries_total == 16
    assert job.entries_done == 16
    # 16 entries walked, 14 files actually transferred — this capture is an adopt run,
    # where the whole point is that most of the list is skipped.
    assert job.files_sent == 14


def test_summary_lines_are_not_mistaken_for_progress():
    for line in LOG:
        if line.startswith("sent ") or line.startswith("total size"):
            assert PROGRESS_RE.match(line) is None


def test_filenames_are_not_mistaken_for_progress():
    paths = [ln for ln in LOG if ln.startswith("Science/")]
    assert paths
    for line in paths:
        assert PROGRESS_RE.match(line) is None


def test_file_events_are_declared_not_guessed():
    """We used to sniff which lines looked like filenames, with a list of rsync's known
    chatter. rsync will instead tell us exactly, via --out-format."""
    from libnodes.jobs import FILE_RE

    m = FILE_RE.match("@3000000|Science/Chess/Tal.pdf")
    assert m is not None
    assert m.group("name") == "Science/Chess/Tal.pdf"
    assert m.group("size") == "3000000"

    # A directory, told apart by its trailing slash rather than by %i — which would
    # have made rsync log every unchanged file too.
    d = FILE_RE.match("@4096|Science/Chess/")
    assert d.group("name").endswith("/")

    # Nothing else can be mistaken for one — not even a filename full of percent signs.
    for line in LOG:
        assert FILE_RE.match(line) is None, line
    assert FILE_RE.match("100% done.pdf") is None
    assert FILE_RE.match("sent 2.30M bytes  received 66 bytes") is None


# ------------------------------------------------- output is specified --


def test_libnodes_specifies_its_own_output_format(app, settings):
    """Parsing must not depend on what the user put in rsync_flags.

    devices.yaml is hand-edited; if someone drops -v or adds --info=name0, the file
    lines change shape and a heuristic parser quietly stops seeing them.
    """
    from libnodes.jobs import INFO_FLAGS, OUT_FORMAT, build_argv

    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"]
    argv = build_argv(device, lib.devices.config, ["Science"], settings)

    assert f"--out-format={OUT_FORMAT}" in argv
    assert f"--info={INFO_FLAGS}" in argv


def test_config_cannot_override_the_transfer_flags(app, settings):
    """rsync_flags is accepted for old files but has no effect.

    The program depends on the exact flags; a hand-edited value that dropped -L or the
    out-format would break it in ways that look like application bugs.
    """
    from libnodes.jobs import BASE_FLAGS, OUT_FORMAT, build_argv

    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"].model_copy(
        update={"rsync_flags": ["-z", "--out-format=%n", "--no-links"]}
    )
    argv = build_argv(device, lib.devices.config, ["Science"], settings)

    assert "-z" not in argv and "--no-links" not in argv
    assert "--out-format=%n" not in argv
    assert f"--out-format={OUT_FORMAT}" in argv
    for flag in BASE_FLAGS:
        assert flag in argv


async def test_file_events_reach_the_dock_as_names(app, tmp_path, monkeypatch):
    """End to end: a declared file event becomes the dock's current file."""
    fake = tmp_path / "r"
    fake.write_text(
        "#!/bin/sh\n"
        "echo '@3000000|Science/Chess/Tal.pdf'\n"
        "echo '@4096|Science/Chess/'\n"
        "printf '   3.00M 100%%  50MB/s  0:00:01 (xfr#1, to-chk=0/2)\\r'\n"
        "echo ''\n"
        "echo 'sent 3.00M bytes  received 66 bytes'\n"
        "exit 0\n"
    )
    fake.chmod(0o755)

    async with app.router.lifespan_context(app):
        monkeypatch.setattr("libnodes.jobs.build_argv", lambda *a, **k: [str(fake)])
        lib = app.state.lib
        job = lib.jobs.submit(lib.devices.config.by_id["kobo"], ["Science"])
        for _ in range(80):
            if lib.jobs.get(job.id).finished:
                break
            await asyncio.sleep(0.05)

        done = lib.jobs.get(job.id)
        # The file, not the directory that followed it.
        assert done.current_file == "Science/Chess/Tal.pdf"
        assert done.pct == 100.0

        rendered = " ".join(text for _css, text in lib.jobs.terminal(job.id))
        assert "Science/Chess/Tal.pdf" in rendered
        assert "3,000,000" in rendered          # size shown alongside
        assert "@3000000" not in rendered       # ...but not the raw marker


def test_directory_times_are_omitted(app, settings):
    """-O keeps the log about files.

    Without it rsync stamps every directory's mtime, counts each as touched and reports
    it — 3,839 directory lines around the 4 files that actually mattered, measured on a
    real device. Directory timestamps mean nothing on a FAT card.
    """
    from libnodes.jobs import build_argv

    lib = app.state.lib
    argv = build_argv(
        lib.devices.config.by_id["kobo"], lib.devices.config, ["Science"], settings
    )
    assert "-O" in argv


def test_perms_are_skipped_on_filesystems_that_cannot_store_them(app, settings):
    """Kobo onboard storage and Android SD cards are FAT: rsync's chmod fails every run
    and it then counts every file as changed. Measured: one directory reported 43 items
    with perms on, 0 with them off."""
    from libnodes.jobs import build_argv

    cfg = app.state.lib.devices.config
    for device_id in ("kobo", "phone"):        # kobo + termux fixtures
        argv = build_argv(cfg.by_id[device_id], cfg, ["Science"], settings)
        assert "--no-perms" in argv, device_id


def test_a_filesystem_without_perms_gets_no_owner_either(app, settings):
    """--no-perms alone leaves -a's -o and -g on, and FAT cannot store those either.

    The Kobo's vfat driver returns EPERM to chown even for root, so job #9 delivered all
    5 books byte-for-byte and still exited 23 on 8 `rsync: chown ... failed: Operation
    not permitted (1)` lines — one per directory and one per temp file — then retried to
    attempt=3. Worse than the exit code: a file whose chown failed keeps the transfer
    time as its mtime, so it fails the next quick check and is re-sent for ever.
    Measured against the live device on 5 identical books — `--no-perms` alone re-sent
    all 5 (xfr#5, exit 23); with all three the same run repaired the mtimes (xfr#5, exit
    0) and the one after it sent 415 bytes (xfr#0). Android's sdcardfs fakes chown as a
    no-op, which is why every nexus10 log is clean and this survived until a real FAT
    driver saw it. All three flags or none.
    """
    from libnodes.jobs import build_argv

    cfg = app.state.lib.devices.config
    for device_id in ("kobo", "phone"):
        argv = build_argv(cfg.by_id[device_id], cfg, ["Science"], settings)
        assert {"--no-perms", "--no-owner", "--no-group"} <= set(argv), device_id


def test_perms_are_kept_for_a_real_filesystem(app, settings, devices_file):
    """A Linux target has real permissions worth preserving."""
    from libnodes.models import parse_devices
    from libnodes.jobs import build_argv

    cfg, _ = parse_devices(
        "devices:\n  - id: mirror\n    name: Mirror\n    type: linux\n"
        "    host: h\n    target: /srv/books\n"
    )
    argv = build_argv(cfg.by_id["mirror"], cfg, ["Science"], settings)
    assert "--no-perms" not in argv
    # The owner half of that decision travels with it: ext4 stores a real uid/gid and
    # -a is there to preserve it.
    assert "--no-owner" not in argv
    assert "--no-group" not in argv


def test_the_filesystem_decides_not_the_device_type(app, settings):
    """A Linux host can have an exFAT disk; an Android app dir can be ext4."""
    from libnodes.models import parse_devices
    from libnodes.jobs import build_argv

    cfg, issues = parse_devices(
        "devices:\n  - id: a\n    name: A\n    type: linux\n    host: h\n"
        "    target: /mnt/card\n    fs: exfat\n"
        "  - id: b\n    name: B\n    type: termux\n    host: h\n"
        "    target: /data/x\n    fs: ext4\n"
    )
    assert issues == []
    assert "--no-perms" in build_argv(cfg.by_id["a"], cfg, ["Science"], settings)
    assert "--no-perms" not in build_argv(cfg.by_id["b"], cfg, ["Science"], settings)


def test_fs_is_normalised_and_unknown_values_are_safe(app, settings):
    """An unrecognised filesystem gets full archive semantics — assume it behaves and
    let a failing sync say otherwise, rather than silently weakening every transfer."""
    from libnodes.models import parse_devices
    from libnodes.jobs import build_argv

    cfg, issues = parse_devices(
        "devices:\n  - id: a\n    name: A\n    host: h\n    target: /t\n"
        "    fs: '  VFAT '\n"
        "  - id: b\n    name: B\n    host: h\n    target: /t\n    fs: reiserfs\n"
    )
    assert issues == []
    assert cfg.by_id["a"].effective_fs == "vfat"
    assert "--no-perms" in build_argv(cfg.by_id["a"], cfg, ["S"], settings)
    assert "--no-perms" not in build_argv(cfg.by_id["b"], cfg, ["S"], settings)


async def test_fat32_size_limit_is_flagged_before_pushing(client, app, index, settings):
    """A 4 GiB file cannot land on FAT32. Better said in the dialog than discovered
    when the transfer dies at 99%."""
    from libnodes.models import FS_PROFILES

    assert FS_PROFILES["vfat"].max_file == 4 * 1024**3 - 1

    # A file bigger than FAT32 can hold.
    big = settings.library_root / "Fiction" / "huge.bin"
    big.write_bytes(b"")
    import os

    os.truncate(big, 5 * 1024**3)
    app.state.lib.index.reindex()

    r = await client.get("/jobs/picker", params={"path": "Fiction/huge.bin"})
    assert "cannot hold a file over" in r.text
    os.remove(big)


async def test_no_size_warning_for_a_filesystem_without_a_limit(client, app):
    r = await client.get("/jobs/picker", params={"path": "Science/Physics"})
    assert "cannot hold a file over" not in r.text


async def test_a_directory_selection_is_measured_by_its_largest_file(
    client, app, settings
):
    """The regression: `Entry.size` on a directory is its recursive total, so pushing
    directories warned about FAT32 using the size of the whole subtree.

    On the real library that read "the largest here is 68.7 GB" — `Science` entire —
    against a biggest actual file of 786 MB. Selecting whole directories is the normal
    case, so the warning was permanently, confidently wrong.

    Two 3 GiB files is the shape that separates the two readings: the directory totals
    6 GiB, over FAT32's limit, while no single file comes near it. Sparse, so this costs
    no disk.
    """
    import os

    big = settings.library_root / "Fiction" / "Bulky"
    big.mkdir()
    for name in ("a.bin", "b.bin"):
        path = big / name
        path.write_bytes(b"")
        os.truncate(path, 3 * 1024**3)
    app.state.lib.index.reindex()

    entry = app.state.lib.index.entry("Fiction/Bulky")
    assert entry.size > 4 * 1024**3, "fixture does not reproduce the bug's precondition"

    r = await client.get("/jobs/picker", params={"path": "Fiction/Bulky"})
    assert r.status_code == 200
    assert "cannot hold a file over" not in r.text, "warned on the directory's total"

    # ...and the warning still fires for a file that genuinely cannot land.
    os.truncate(big / "a.bin", 5 * 1024**3)
    app.state.lib.index.reindex()
    r = await client.get("/jobs/picker", params={"path": "Fiction/Bulky"})
    assert "cannot hold a file over" in r.text, "lost the warning it exists to give"


async def test_the_index_measures_the_largest_file_not_the_subtree(index):
    """The arithmetic itself, away from the dialog: the largest file anywhere under
    `Science`, including its subdirectories, and never the subtree's total."""
    def every_file(path):
        for entry in index.children(path, sort="name"):
            if entry.is_dir:
                yield from every_file(entry.path)
            else:
                yield entry.size

    expected = max(every_file("Science"))

    assert index.max_file_size(["Science"]) == expected
    assert index.max_file_size(["Science"]) < index.entry("Science").size


async def test_the_largest_file_query_spans_every_selected_path(index):
    """One job per device, but one warning for the whole selection — so the query has to
    consider all of the chosen paths, not just the first."""
    physics = index.max_file_size(["Science/Physics"])
    chess = index.max_file_size(["Science/Chess"])
    both = index.max_file_size(["Science/Chess", "Science/Physics"])
    assert both == max(physics, chess)
    assert index.max_file_size([]) == 0
