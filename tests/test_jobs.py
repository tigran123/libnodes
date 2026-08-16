"""rsync invocation, progress parsing, and the runner end to end."""

from __future__ import annotations

import asyncio

import pytest

from libnodes.jobs import PROGRESS_RE, Job, build_argv, full_sync_sources


def _device(app, device_id="kobo"):
    return app.state.lib.devices.config.by_id[device_id]


def test_argv_forces_copy_links(app, settings):
    """The single most important line in the app.

    The library is symlinks into /Books/.data. Without -L the device receives dangling
    links instead of books, and rsync reports success while doing so.
    """
    lib = app.state.lib
    argv = build_argv(
        _device(app), lib.devices.config, ["Science/Physics"], settings
    )
    assert "-L" in argv


def test_argv_uses_relative_so_structure_survives(app, settings):
    lib = app.state.lib
    argv = build_argv(_device(app), lib.devices.config, ["Science/Physics"], settings)
    assert "-R" in argv
    assert "Science/Physics/" in argv
    assert argv[-1] == "root@127.0.0.1:/mnt/onboard/Books/"


def test_argv_is_a_list_never_a_shell_string(app, settings):
    lib = app.state.lib
    argv = build_argv(_device(app), lib.devices.config, ["Science/Physics"], settings)
    assert all(isinstance(a, str) for a in argv)
    assert argv[0] == "rsync"
    # The -e value is one argument carrying the whole ssh command.
    ssh = argv[argv.index("-e") + 1]
    assert ssh.startswith("ssh -p 2222")
    assert "BatchMode=yes" in ssh


def test_argv_honours_explicit_flags_without_duplicating(app, settings):
    lib = app.state.lib
    device = _device(app).model_copy(update={"rsync_flags": ["-a", "-L", "-R"]})
    argv = build_argv(device, lib.devices.config, ["Science"], settings)
    assert argv.count("-L") == 1
    assert argv.count("-R") == 1


def test_dry_run_adds_n(app, settings):
    lib = app.state.lib
    argv = build_argv(
        _device(app), lib.devices.config, ["Science"], settings, dry_run=True
    )
    assert "-n" in argv


def test_full_sync_sources_skip_infrastructure(settings):
    sources = full_sync_sources(settings)
    assert "Science" in sources and "Fiction" in sources
    assert "urantia-library" not in sources
    assert ".data" not in sources


# --------------------------------------------------------------- parsing --


@pytest.mark.parametrize(
    "line,expected",
    [
        (
            "        1,234,567  45%   11.34MB/s    0:00:12 (xfr#3, to-chk=9/12)",
            (1234567, 45, "11.34MB/s", 3, 9, 12),
        ),
        (
            "  2,610,432  41%    8.90MB/s    0:00:06 (xfr#12, to-chk=0/12)",
            (2610432, 41, "8.90MB/s", 12, 0, 12),
        ),
        # Early in a large transfer rsync reports ir-chk while still building the list.
        (
            "     32,768   0%   31.25kB/s    0:00:00 (xfr#1, ir-chk=1015/1016)",
            (32768, 0, "31.25kB/s", 1, 1015, 1016),
        ),
    ],
)
def test_progress_regex(line, expected):
    m = PROGRESS_RE.match(line)
    assert m is not None
    got = (
        int(m.group(1).replace(",", "")),
        int(m.group(2)),
        m.group(3),
        int(m.group(5)),
        int(m.group(6)),
        int(m.group(7)),
    )
    assert got == expected


def test_progress_regex_ignores_filenames():
    assert PROGRESS_RE.match("Fiction/Joyce/Ulysses.pdf") is None
    assert PROGRESS_RE.match("sending incremental file list") is None


def test_to_chk_is_entries_and_xfr_is_files():
    """The two numbers in a progress line count different things.

    `to-chk` is file-list entries walked past — skipped files and directories included.
    `xfr#` is transfers completed. Reading the first as the second reported 35 files
    sent for a real run that had sent 15.
    """
    from libnodes.jobs import _apply_progress

    job = Job(id=1, device_id="kobo", sources=[], label="x")
    m = PROGRESS_RE.match(
        "        1,234,567  45%   11.34MB/s    0:00:12 (xfr#3, to-chk=9/12)"
    )
    _apply_progress(job, m)
    assert job.entries_total == 12
    assert job.entries_done == 3
    assert job.files_sent == 3
    # The bar tracks entries, not rsync's byte percentage: 45% of the file list's total
    # size says nothing useful when most of the list is being skipped.
    assert job.pct == pytest.approx(25.0)


def test_progress_never_overwrites_the_index_estimate():
    """files_total/bytes_total mean "what was selected" for the whole life of the job.

    Letting rsync redefine them mid-run is what made one directory read 234 files in the
    Library tree and 244 in the dock — rsync counts its 9 subdirectories and itself.
    """
    from libnodes.jobs import _apply_progress

    job = Job(
        id=1, device_id="kobo", sources=[], label="x",
        files_total=234, bytes_total=9_923_801_623,
    )
    m = PROGRESS_RE.match(
        "      302,618,249   3%   23.57MB/s    0:00:13 (xfr#14, to-chk=209/244)"
    )
    _apply_progress(job, m)
    assert job.files_total == 234
    assert job.bytes_total == 9_923_801_623
    assert job.entries_total == 244
    assert job.files_sent == 14


def test_a_no_op_sync_reports_nothing_sent():
    """Skipping 24,620 entries must not look like transferring 24,620 files."""
    from libnodes.jobs import _apply_progress

    job = Job(id=1, device_id="kobo", sources=[], label="x", files_total=24_620)
    for remaining in (12_000, 0):
        m = PROGRESS_RE.match(
            f"                0   0%    0.00kB/s    0:00:00 (xfr#0, to-chk={remaining}/24620)"
        )
        _apply_progress(job, m)
    assert job.files_sent == 0
    assert job.bytes_done == 0
    assert job.pct == 100  # the walk finished, and the bar says so
    assert job.entries_done == 24_620


# ------------------------------------------------------------ end to end --


async def test_runner_streams_progress_and_completes(app, fake_rsync, monkeypatch):
    """Drive the whole runner with a fake rsync: queue → progress → done → manifest."""
    lib = app.state.lib
    async with app.router.lifespan_context(app):
        device = _device(app)
        monkeypatch.setattr(
            "libnodes.jobs.build_argv",
            lambda *a, **k: [str(fake_rsync)],
        )
        events = lib.jobs.subscribe()
        job = lib.jobs.submit(device, ["Fiction"])

        for _ in range(100):
            current = lib.jobs.get(job.id)
            if current.finished:
                break
            await asyncio.sleep(0.05)

        finished = lib.jobs.get(job.id)
        assert finished.state == "done", finished.error
        assert finished.exit_code == 0
        assert finished.pct == 100.0

        kinds = set()
        while not events.empty():
            kinds.add(events.get_nowait().kind)
        assert {"line", "done"} <= kinds

        # The push updated the manifest, so PRESENT ON is accurate afterwards.
        count, _, _ = lib.manifests.summary("kobo")
        assert count == 2  # both Fiction files


async def _drain(lib, job_id, tries=100):
    for _ in range(tries):
        if lib.jobs.get(job_id).finished:
            break
        await asyncio.sleep(0.05)
    return lib.jobs.get(job_id)


@pytest.fixture
def interrupted_rsync(tmp_path):
    """A push that delivers two files, starts a third, and is killed.

    Modelled on a real aborted run: rsync prints a file's name when it *starts* sending
    it, so the log ends with an @-line for a file that never landed. The directory
    @-line is in here too, because those must not be counted as transfers either.
    """
    script = tmp_path / "interrupted-rsync"
    script.write_text(
        "#!/bin/sh\n"
        "echo '@4096|Science/Physics/'\n"
        "echo '@720|Science/Physics/Feynman.djvu'\n"
        "printf '        720  10%%   0.47MB/s    0:00:00 (xfr#1, to-chk=3/5)\\r'\n"
        "echo '@1200|Science/Physics/Landau.pdf'\n"
        "printf '      1,920  30%%   0.47MB/s    0:00:01 (xfr#2, to-chk=2/5)\\r'\n"
        "echo '@99|Science/Chess/Tal.pdf'\n"
        "exit 20\n"
    )
    script.chmod(0o755)
    return script


async def test_aborted_push_credits_only_the_files_that_landed(
    app, interrupted_rsync, monkeypatch
):
    """Aborting used to discard everything the run achieved.

    PRESENT ON then described a device state that had not been true since before the
    push. Record what rsync confirmed it sent — and nothing else.
    """
    lib = app.state.lib
    async with app.router.lifespan_context(app):
        monkeypatch.setattr(
            "libnodes.jobs.build_argv", lambda *a, **k: [str(interrupted_rsync)]
        )
        job = lib.jobs.submit(_device(app), ["Science"])
        finished = await _drain(lib, job.id)

        assert finished.state == "aborted"
        assert finished.files_sent == 2

        paths = lib.manifests.paths_for("kobo")
        assert paths == {
            "Science/Physics/Feynman.djvu",
            "Science/Physics/Landau.pdf",
        }
        # The in-flight file is excluded: --partial may have left it truncated under its
        # final name, and a size-mismatched row still reads as present.
        assert "Science/Chess/Tal.pdf" not in paths
        # A directory @-line is not a transfer.
        assert "Science/Physics" not in paths


async def test_aborted_dry_run_records_nothing(app, interrupted_rsync, monkeypatch):
    """A dry run moves no data however it ends."""
    lib = app.state.lib
    async with app.router.lifespan_context(app):
        monkeypatch.setattr(
            "libnodes.jobs.build_argv", lambda *a, **k: [str(interrupted_rsync)]
        )
        job = lib.jobs.submit(_device(app), ["Science"], dry_run=True)
        await _drain(lib, job.id)
        assert lib.manifests.paths_for("kobo") == set()


async def test_each_retry_credits_what_it_delivered(app, tmp_path, monkeypatch):
    """A retry starts rsync over, so it skips what the previous attempt landed.

    Those files are never named again. Crediting only the final attempt would drop
    everything the earlier ones achieved, which for a flaky link is most of the push.
    """
    lib = app.state.lib
    # Attempt 1 delivers Feynman, attempt 2 delivers Landau; each then dies.
    counter = tmp_path / "attempt"
    script = tmp_path / "flaky-rsync"
    script.write_text(
        "#!/bin/sh\n"
        f"n=$(cat {counter} 2>/dev/null || echo 1)\n"
        f"echo $((n + 1)) > {counter}\n"
        "if [ \"$n\" = 1 ]; then f=Science/Physics/Feynman.djvu; "
        "else f=Science/Physics/Landau.pdf; fi\n"
        "echo \"@720|$f\"\n"
        "printf '        720  10%%   0.4MB/s    0:00:00 (xfr#1, to-chk=3/5)\\r'\n"
        "exit 12\n"
    )
    script.chmod(0o755)

    async with app.router.lifespan_context(app):
        monkeypatch.setattr("libnodes.jobs.build_argv", lambda *a, **k: [str(script)])
        monkeypatch.setattr(lib.jobs, "_retries_for", lambda job: 1)
        job = lib.jobs.submit(_device(app), ["Science"])
        finished = await _drain(lib, job.id, tries=200)

        assert finished.state == "failed"
        assert finished.attempt > 1, "fixture device must be configured with retries"
        assert lib.manifests.paths_for("kobo") == {
            "Science/Physics/Feynman.djvu",
            "Science/Physics/Landau.pdf",
        }


async def test_wire_bytes_come_from_the_closing_summary(app, tmp_path, monkeypatch):
    """progress2's byte counter is file size, not network traffic.

    When the device already holds a copy, rsync's delta algorithm reconstructs it from
    what is there and sends almost nothing: a real push reported 4,379,115,438 bytes
    while putting 6.7 MB on the link. Only the closing `sent … received …` line knows
    the difference.
    """
    lib = app.state.lib
    script = tmp_path / "delta-rsync"
    script.write_text(
        "#!/bin/sh\n"
        "echo '@1200|Science/Physics/Landau.pdf'\n"
        "printf '  4,379,115,438  44%%   18.88MB/s    0:03:41 (xfr#1, to-chk=0/5)\\r'\n"
        "echo ''\n"
        "echo 'sent 2,491,047 bytes  received 4,203,364 bytes  25,997.71 bytes/sec'\n"
        "echo 'total size is 9,923,801,623  speedup is 1,482.40'\n"
        "exit 0\n"
    )
    script.chmod(0o755)

    async with app.router.lifespan_context(app):
        monkeypatch.setattr("libnodes.jobs.build_argv", lambda *a, **k: [str(script)])
        job = lib.jobs.submit(_device(app), ["Science"])
        finished = await _drain(lib, job.id)

        assert finished.bytes_done == 4_379_115_438   # size of the files handled
        assert finished.bytes_wire == 6_694_411       # 2,491,047 + 4,203,364
        assert finished.files_sent == 1


async def test_dock_separates_files_sent_from_entries_checked(
    client, app, interrupted_rsync, monkeypatch
):
    """The dock said "35/244 files" for a run that had sent 15 out of 234.

    Both halves of that were wrong: 35 was entries walked past, and 244 counted the
    directories rsync had to create. Sent and checked are now different lines, and the
    one holding rsync's total says "entries" so the directories in it are visible.
    """
    lib = app.state.lib
    async with app.router.lifespan_context(app):
        monkeypatch.setattr(
            "libnodes.jobs.build_argv", lambda *a, **k: [str(interrupted_rsync)]
        )
        job = lib.jobs.submit(_device(app), ["Science"])
        await _drain(lib, job.id)

        r = await client.get(f"/jobs/dock?active={job.id}")
        assert "2 files sent" in r.text
        assert "entries" in r.text
        # The old conflated readout, in any of its forms.
        assert "3/5 files" not in r.text
        assert "5 files" not in r.text


async def test_failed_job_is_recorded_with_exit_code(app, tmp_path, monkeypatch):
    lib = app.state.lib
    failing = tmp_path / "failing-rsync"
    failing.write_text("#!/bin/sh\necho 'rsync: connection unexpectedly closed' >&2\nexit 12\n")
    failing.chmod(0o755)

    async with app.router.lifespan_context(app):
        monkeypatch.setattr("libnodes.jobs.build_argv", lambda *a, **k: [str(failing)])
        job = lib.jobs.submit(_device(app), ["Fiction"])
        for _ in range(100):
            if lib.jobs.get(job.id).finished:
                break
            await asyncio.sleep(0.05)

        finished = lib.jobs.get(job.id)
        assert finished.state == "failed"
        assert finished.exit_code == 12
        # A failed push must not claim the files landed.
        assert lib.manifests.summary("kobo")[0] == 0


async def test_log_is_readable_while_the_job_runs(app, tmp_path, monkeypatch, settings):
    """The regression: the log was block-buffered, so a running job's log was empty.

    That is precisely when you want to read it — a transfer that has been going for ten
    minutes and looks stuck.
    """
    slow = tmp_path / "slow-rsync"
    slow.write_text(
        "#!/bin/sh\n"
        "echo 'sending incremental file list'\n"
        "echo 'Fiction/first.epub'\n"
        "sleep 2\n"
        "echo 'Fiction/second.epub'\n"
        "exit 0\n"
    )
    slow.chmod(0o755)

    async with app.router.lifespan_context(app):
        monkeypatch.setattr("libnodes.jobs.build_argv", lambda *a, **k: [str(slow)])
        lib = app.state.lib
        job = lib.jobs.submit(_device(app), ["Fiction"])

        log = settings.logs_dir / f"{job.id}.log"
        for _ in range(40):
            await asyncio.sleep(0.05)
            if log.exists() and "first.epub" in log.read_text():
                break
        else:
            pytest.fail("nothing reached the log while the job was still running")

        assert not lib.jobs.get(job.id).finished, "job finished before we could check"


async def test_log_is_written(app, fake_rsync, monkeypatch, settings):
    lib = app.state.lib
    async with app.router.lifespan_context(app):
        monkeypatch.setattr("libnodes.jobs.build_argv", lambda *a, **k: [str(fake_rsync)])
        job = lib.jobs.submit(_device(app), ["Fiction"])
        for _ in range(100):
            if lib.jobs.get(job.id).finished:
                break
            await asyncio.sleep(0.05)

    log = settings.logs_dir / f"{job.id}.log"
    assert log.exists()
    assert "sending incremental file list" in log.read_text()


async def test_iter_lines_splits_on_carriage_returns():
    """--info=progress2 rewrites one line in place; readline() would block for ever."""
    from libnodes.jobs import _iter_lines

    reader = asyncio.StreamReader()
    reader.feed_data(b"a\rb\rc\nd\n")
    reader.feed_eof()
    assert [line async for line in _iter_lines(reader)] == ["a", "b", "c", "d"]
