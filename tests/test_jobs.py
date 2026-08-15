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


def test_files_done_derived_from_to_chk():
    from libnodes.jobs import _apply_progress

    job = Job(id=1, device_id="kobo", sources=[], label="x")
    m = PROGRESS_RE.match(
        "        1,234,567  45%   11.34MB/s    0:00:12 (xfr#3, to-chk=9/12)"
    )
    _apply_progress(job, m)
    assert job.files_total == 12
    assert job.files_done == 3
    assert job.pct == 45
    # bytes_total is back-derived from the percentage rsync reports.
    assert job.bytes_total == pytest.approx(1234567 * 100 / 45, rel=0.01)


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
