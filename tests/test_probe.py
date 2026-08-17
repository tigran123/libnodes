"""Reachability probing must never make the UI wait on a dead device."""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from libnodes.probe import DeviceProbe, FreeSpace, Reachability, _parse_df


def _probe(app) -> DeviceProbe:
    return app.state.lib.probe


def _device(app, device_id="kobo"):
    return app.state.lib.devices.config.by_id[device_id]


# ----------------------------------------------------------------- backoff --


def test_backoff_grows_then_caps(app, settings):
    from libnodes.config import DevicesStore
    from libnodes.probe import DeviceProbe

    # The fixture uses a 1h interval to keep tests quiet, which is already past the cap;
    # use a realistic interval so the growth is observable.
    tuned = settings.model_copy(update={"probe_interval": 10.0, "probe_backoff_max": 300.0})
    probe = DeviceProbe(tuned, DevicesStore(settings.resolved_devices_file))

    delays = [probe._backoff(n) for n in range(1, 12)]
    assert delays[0] == 10.0
    assert delays[:5] == [10.0, 20.0, 40.0, 80.0, 160.0]
    assert delays == sorted(delays)  # monotonic
    assert delays[-1] == 300.0
    assert all(d <= 300.0 for d in delays)


def test_backoff_never_exceeds_the_cap_even_if_interval_does(settings):
    """A long probe_interval must not multiply past the ceiling."""
    from libnodes.config import DevicesStore
    from libnodes.probe import DeviceProbe

    tuned = settings.model_copy(update={"probe_interval": 3600.0, "probe_backoff_max": 300.0})
    probe = DeviceProbe(tuned, DevicesStore(settings.resolved_devices_file))
    assert all(probe._backoff(n) == 300.0 for n in range(1, 10))


async def test_repeated_failures_push_the_next_probe_out(app, monkeypatch):
    """Twenty dead devices must not cost twenty connects every 10 seconds."""
    probe = _probe(app)
    device = _device(app)

    async def refuse(*a, **k):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr("asyncio.open_connection", refuse)

    await probe.probe(device)
    first = probe.status(device.id)
    assert first.state == "offline"
    assert first.failures == 1

    for _ in range(5):
        await probe.probe(device)
    later = probe.status(device.id)
    assert later.failures == 6
    # Now due far in the future rather than in probe_interval seconds.
    assert later.next_probe_at - time.time() > 60


async def test_backoff_skips_undue_devices(app, monkeypatch):
    probe = _probe(app)
    calls = []

    async def refuse(host, port, *a, **k):
        calls.append(host)
        raise OSError(113, "No route to host")

    monkeypatch.setattr("asyncio.open_connection", refuse)

    await probe.probe_all()
    first_round = len(calls)
    assert first_round == 2  # both fixture devices

    # Immediately again: everything is backed off, so nothing is attempted.
    await probe.probe_all()
    assert len(calls) == first_round

    # ...unless forced, which is what Rescan means.
    await probe.probe_all(force=True)
    assert len(calls) == first_round + 2


def test_a_watched_fleet_is_retried_sooner(settings):
    """The ceiling is a cost control, and its cost is paid by whoever is looking at it."""
    from libnodes.config import DevicesStore
    from libnodes.probe import DeviceProbe

    tuned = settings.model_copy(
        update={
            "probe_interval": 10.0,
            "probe_backoff_max": 300.0,
            "probe_backoff_watched": 30.0,
            "watch_window": 60.0,
        }
    )
    probe = DeviceProbe(tuned, DevicesStore(settings.resolved_devices_file))

    # Nobody has looked since startup: the long ceiling applies.
    assert probe.watched is False
    assert probe._backoff(9) == 300.0

    probe.note_interest()
    assert probe.watched is True
    assert probe._backoff(9) == 30.0
    # The early growth is untouched -- only the ceiling moves.
    assert probe._backoff(1) == 10.0
    assert probe._backoff(2) == 20.0

    # ...and it lapses on its own once the page stops polling.
    probe._interest_at = time.time() - 61.0
    assert probe.watched is False
    assert probe._backoff(9) == 300.0


def test_the_watch_window_outlasts_a_backgrounded_tab(settings):
    """`every 10s` is what a *foreground* tab does. Backgrounded, the browser throttles
    the timer to once a minute — measured on the Pi's journal, the same tab dropping from
    10s to exactly 60s intervals. A window of 60 would sit on that boundary and flap
    between the two ceilings, so a device would be retried at whichever rate happened to
    apply when `_backoff` ran."""
    assert settings.watch_window > 60.0, (
        "a backgrounded Devices tab polls once a minute; a window at or below that "
        "flaps the backoff ceiling"
    )


async def test_an_opening_tab_pulls_a_long_backoff_forward(app, monkeypatch):
    """The regression: a device that came back stayed red for five minutes.

    It failed while nobody was watching, so `next_probe_at` was stamped 300s out. Opening
    the page must not mean waiting out an appointment made under the other ceiling.
    """
    probe = _probe(app)
    device = _device(app)
    probe.settings = probe.settings.model_copy(
        update={
            "probe_interval": 10.0,
            "probe_backoff_max": 300.0,
            "probe_backoff_watched": 30.0,
            "watch_window": 60.0,
        }
    )

    async def refuse(*a, **k):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr("asyncio.open_connection", refuse)
    for _ in range(9):
        await probe.probe(device)

    reach = probe.status(device.id)
    assert reach.next_probe_at - time.time() > 250  # parked at the long ceiling

    # Still parked 40s later, with nobody watching.
    later = time.time() + 40.0
    assert probe.due(device, now=later) is False

    # A Devices page opens. The same 40s is now past the watched ceiling, so the device
    # is due immediately -- without waiting for the stored appointment.
    probe.note_interest()
    assert probe.due(device, now=later) is True


async def test_note_interest_touches_no_device(app, monkeypatch):
    """It runs inside a request, so it must be a stamp and nothing else."""
    probe = _probe(app)

    async def explode(*a, **k):
        raise AssertionError("a request opened a connection to a device")

    monkeypatch.setattr("asyncio.open_connection", explode)

    before = probe._interest_at
    probe.note_interest()
    assert probe._interest_at > before


async def test_the_devices_page_marks_the_fleet_watched(client, app):
    """The row poll alone must hold the shorter ceiling -- no extra request needed."""
    probe = _probe(app)
    probe._interest_at = 0.0

    resp = await client.get("/devices/rows")
    assert resp.status_code == 200
    assert probe.watched is True


async def test_success_resets_the_backoff(app, monkeypatch):
    probe = _probe(app)
    device = _device(app)

    async def refuse(*a, **k):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr("asyncio.open_connection", refuse)
    for _ in range(4):
        await probe.probe(device)
    assert probe.status(device.id).failures == 4

    class _W:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def accept(*a, **k):
        return (None, _W())

    monkeypatch.setattr("asyncio.open_connection", accept)
    await probe.probe(device)
    assert probe.status(device.id).state == "online"
    assert probe.status(device.id).failures == 0


async def test_the_sweep_is_not_delayed_by_df(app, monkeypatch):
    """A `df` is bounded at 15s and tried twice. Awaiting it in the loop put up to 30s
    per online node between reachability sweeps — 30s of every dot being stale."""
    probe = _probe(app)
    probe.settings = probe.settings.model_copy(update={"probe_interval": 0.01})

    class _W:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def accept(*a, **k):
        return (None, _W())

    monkeypatch.setattr("asyncio.open_connection", accept)

    spawned = []

    async def slow_space(device, force=False):
        spawned.append(device.id)
        await asyncio.sleep(30)

    monkeypatch.setattr(probe, "probe_space", slow_space)

    # One tick of the real loop, with a df that would block it for half a minute.
    task = asyncio.create_task(probe._loop())
    try:
        await asyncio.sleep(0.2)
        # The sweep ran repeatedly rather than parking inside the first df...
        assert probe.status("kobo").state == "online"
        assert probe.status("phone").state == "online"
        # ...and the df was started, just never waited for.
        assert spawned, "the space probe was not spawned at all"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_wedged_loop_says_so(app, monkeypatch, caplog):
    """A body that always raises leaves the fleet grey and /healthz at `online: 0` —
    indistinguishable from a genuinely dark fleet unless it is logged."""
    probe = _probe(app)
    probe.settings = probe.settings.model_copy(update={"probe_interval": 0.01})

    async def boom(*a, **k):
        raise RuntimeError("devices.yaml is unreadable")

    monkeypatch.setattr(probe, "probe_all", boom)

    task = asyncio.create_task(probe._loop())
    try:
        with caplog.at_level("WARNING", logger="libnodes.probe"):
            await asyncio.sleep(0.2)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    hits = [r for r in caplog.records if "devices.yaml is unreadable" in r.getMessage()]
    assert hits, "a permanently failing probe loop logged nothing"
    # Once, not once per tick: the loop ran many times in that window.
    assert len(hits) == 1, f"logged {len(hits)} times for one unchanging fault"


# ------------------------------------------------------- non-blocking UI --


async def test_rescan_returns_before_the_probe_finishes(client, app, monkeypatch):
    """The regression this guards: Rescan used to await every unreachable node.

    With a handful of dead devices that turned one click into tens of seconds of
    staring at a frozen table.
    """

    async def slow(*a, **k):
        await asyncio.sleep(5)
        raise asyncio.TimeoutError

    monkeypatch.setattr("asyncio.open_connection", slow)

    started = time.perf_counter()
    response = await client.post("/devices/rescan")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 1.0, f"rescan blocked for {elapsed:.1f}s"
    # It schedules its own follow-up rather than leaving the table stale.
    assert "/devices/rows" in response.text


async def test_single_device_probe_is_bounded(client, app, monkeypatch, settings):
    """One node may be awaited — but only for probe_timeout, never for the df probe."""

    async def never(*a, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr("asyncio.open_connection", never)

    started = time.perf_counter()
    response = await client.post("/device/kobo/probe")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < settings.probe_timeout + 1.5, f"probe took {elapsed:.1f}s"


async def test_page_renders_while_every_device_hangs(client, monkeypatch):
    """Requests read the cached dict; they never probe. This is the core rule."""

    async def never(*a, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr("asyncio.open_connection", never)

    for path in ("/devices", "/devices/rows", "/library", "/jobs", "/healthz"):
        started = time.perf_counter()
        response = await client.get(path)
        elapsed = time.perf_counter() - started
        assert response.status_code == 200
        assert elapsed < 1.0, f"{path} blocked for {elapsed:.1f}s"


# ------------------------------------------------------------ state rules --


async def test_recent_contact_reads_as_sleeping_not_offline(app, monkeypatch):
    """Termux sshd stops with the screen; that is asleep, not gone."""
    probe = _probe(app)
    device = _device(app)
    probe._slot(device.id).reach = Reachability(
        state="online", last_ok=time.time(), checked_at=time.time()
    )

    async def refuse(*a, **k):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr("asyncio.open_connection", refuse)
    await probe.probe(device)
    assert probe.status(device.id).state == "sleeping"


async def test_never_seen_device_reads_as_offline(app, monkeypatch):
    probe = _probe(app)

    async def unreachable(*a, **k):
        raise OSError(113, "No route to host")

    monkeypatch.setattr("asyncio.open_connection", unreachable)
    await probe.probe(_device(app))
    status = probe.status("kobo")
    assert status.state == "offline"
    assert status.error == "no route to host"


# ------------------------------------------------------------------- df --


GNU_DF = (
    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
    "/dev/mmcblk0p3 30408704 7969472 22439232 27% /mnt/onboard\n"
)

# Verbatim from the LG Android device over Termux sshd. toybox df rejects -P and -k
# (exit 1) and reports human-readable values, so the original 1K-block parser returned
# None and every Termux node showed "—" for storage.
TOYBOX_DF = (
    "Filesystem                 Size         Used         Free    Blksize\n"
    "/data/data/com.termux/files/home/sd/Books    466.35G      302.90G"
    "      163.46G      32768\n"
)


@pytest.mark.parametrize(
    "text,expected",
    [
        (GNU_DF, (30408704 * 1024, 7969472 * 1024, 22439232 * 1024)),
        (
            TOYBOX_DF,
            (
                int(466.35 * 1024**3),
                int(302.90 * 1024**3),
                int(163.46 * 1024**3),
            ),
        ),
        ("Filesystem\n", None),
        ("", None),
        ("garbage without numbers\nmore garbage\n", None),
    ],
)
def test_parse_df(text, expected):
    assert _parse_df(text) == expected


def test_toybox_df_gives_a_sane_free_figure():
    """The regression: 163 GB free must not be read as 163 KB or as nothing at all."""
    total, used, free = _parse_df(TOYBOX_DF)
    assert free > 160 * 1024**3
    assert total > used > 0
    assert abs((used + free) - total) < total * 0.05


def test_df_line_wrapped_device_name_is_skipped():
    """Without -P, a long device name wraps onto its own line."""
    text = (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/disk/by-uuid/a-very-long-name-indeed\n"
        "           30408704 7969472 22439232 27% /srv\n"
    )
    assert _parse_df(text) == (30408704 * 1024, 7969472 * 1024, 22439232 * 1024)


# --------------------------------------------- the row reports, not guesses --


def _row(html: str, device_id: str) -> str:
    """One row's markup: from its id up to the start of the next row."""
    parts = html.split('id="node-')
    return next(p for p in parts if p.startswith(device_id + '"'))


async def test_a_sleeping_row_prints_the_error_it_actually_got(client, app):
    """The row used to print a fixed "sshd asleep" for every sleeping device, throwing
    the measurement away — so a refused connection on port 2222 was reported as a state
    the probe never established. `sleeping` means only "the connect failed and it
    answered within sleeping_window"; the error is the only thing that says why."""
    lib = app.state.lib
    lib.probe._slot("kobo").reach = Reachability(
        state="sleeping",
        last_ok=time.time(),
        checked_at=time.time(),
        error="connection refused",
    )
    row = _row((await client.get("/devices/rows")).text, "kobo")
    assert "connection refused" in row
    assert "sshd asleep" not in row
    # The reading survives, as a reading: in the tooltip, not asserted as the state.
    assert "Termux&#39;s sshd stops" in row or "Termux's sshd stops" in row


async def test_a_sleeping_timeout_does_not_blame_sshd(client, app):
    """The case the fixed string got wrong. A refused connection means the host is up
    with nothing on the port; a timeout means nothing answered at all, so the device is
    gone rather than its sshd — and "sshd asleep" sent you looking in the wrong place."""
    lib = app.state.lib
    lib.probe._slot("kobo").reach = Reachability(
        state="sleeping",
        last_ok=time.time(),
        checked_at=time.time(),
        error="timed out",
    )
    row = _row((await client.get("/devices/rows")).text, "kobo")
    assert "timed out" in row
    assert "sshd" not in row
    assert "nothing answered at all" in row


async def test_an_offline_row_still_prints_its_error(client, app):
    """The offline branch already did this; the fix must not have cost it."""
    lib = app.state.lib
    lib.probe._slot("kobo").reach = Reachability(
        state="offline", checked_at=time.time(), error="no route to host"
    )
    row = _row((await client.get("/devices/rows")).text, "kobo")
    assert "no route to host" in row
    assert "t-err" in row


async def test_the_row_says_how_old_its_reading_is(client, app):
    """The row is re-rendered every 10s but the probe behind it backs off to five
    minutes, so without the age a dot silently asserts a measurement it did not take:
    a device that came back four minutes ago looks exactly like one still down."""
    lib = app.state.lib
    now = time.time()
    lib.probe._slot("kobo").reach = Reachability(
        state="offline",
        last_ok=now - 7200,
        checked_at=now - 240,
        error="no route to host",
        failures=9,
        next_probe_at=now + 90,
    )
    row = _row((await client.get("/devices/rows")).text, "kobo")
    assert "no route to host" in row      # the fact
    assert "last answered 2h ago" in row  # when it was last up
    assert "checked 4m ago" in row        # how stale this reading is
    assert "next in 1m" in row            # and when that changes


async def test_a_never_checked_row_does_not_claim_a_reading(client, app):
    """`checked_at` is None until the first sweep lands. Printing "checked never ago"
    there would assert exactly the thing the age exists to stop."""
    lib = app.state.lib
    lib.probe._slot("kobo").reach = Reachability(
        state="offline", error="no route to host"
    )
    row = _row((await client.get("/devices/rows")).text, "kobo")
    assert "not checked yet" in row
    assert "checked never" not in row


# ------------------------------------------------ actions follow the dot --


async def test_actions_is_disabled_unless_the_device_is_green(client, app, monkeypatch):
    """Every action behind the menu needs the device to answer. Offering them to a
    device that cannot respond only produces a failure the user could be spared."""
    import time as _time

    from libnodes.probe import Reachability

    lib = app.state.lib

    def row_for(html: str, device_id: str) -> str:
        """One row's markup: from its id up to the start of the next row."""
        parts = html.split('id="node-')
        return next(p for p in parts if p.startswith(device_id + '"'))

    # Offline: greyed out, with Retry offered instead.
    lib.probe._slot("kobo").reach = Reachability(state="offline", checked_at=_time.time())
    kobo = row_for((await client.get("/devices/rows")).text, "kobo")
    assert "is-disabled" in kobo
    assert "/device/kobo/menu" not in kobo
    assert "/device/kobo/probe" in kobo

    # Sleeping is amber, not green — same treatment.
    lib.probe._slot("kobo").reach = Reachability(
        state="sleeping", last_ok=_time.time(), checked_at=_time.time()
    )
    kobo = row_for((await client.get("/devices/rows")).text, "kobo")
    assert "is-disabled" in kobo

    # Green: the menu is live.
    lib.probe._slot("kobo").reach = Reachability(
        state="online", last_ok=_time.time(), checked_at=_time.time()
    )
    kobo = row_for((await client.get("/devices/rows")).text, "kobo")
    assert "/device/kobo/menu" in kobo
    assert "is-disabled" not in kobo


# ------------------------------------------ storage says one thing, not two --


async def test_storage_prints_the_number_the_bar_draws(client, app):
    """The column printed free space under a bar drawing used space.

    LG G4's real card reads "163G / 466G" beside a bar two-thirds full, and the rail
    below it prints `disk 894G / 2.7T` — used over total — for the Pi's own disk. A
    figure and a bar side by side have to be the same quantity.
    """
    lib = app.state.lib
    total, used, free = _parse_df(TOYBOX_DF)      # the real card: 466G, 303G used, 163G free
    now = time.time()
    lib.probe._slot("kobo").reach = Reachability(state="online", last_ok=now, checked_at=now)
    lib.probe._slot("kobo").space = FreeSpace(
        total=total, used=used, free=free, checked_at=now
    )

    pct = 100.0 * used / total                    # 64.9%, and the bar already said so
    for path in ("/devices/rows", "/devices/grid"):
        text = (await client.get(path)).text
        assert "303G / 466G" in text, path
        assert "163G / 466G" not in text, path
        assert f"width:{pct:.1f}%" in text, path
        # Free space still matters before a push; it moved to the tooltip.
        assert "163.5 GB free of" in text, path


def test_the_grid_and_the_table_draw_the_bar_the_same_way():
    """Same datum, same colour. The card used `.disk-bar` (`> i` is --faint) beside a
    table using `.track` (`> i` is --accent), so one device's usage rendered grey in
    GRID and purple in TABLE — under a card comment claiming "same colours"."""
    import re as _re

    from libnodes.templating import TEMPLATES_DIR

    bar = _re.compile(r'<div class="([^"]+)"[^>]*>\s*<i style="width:\{\{[^}]*used_pct')
    classes = {}
    for name in ("device_row.html", "device_grid.html"):
        text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        match = bar.search(text)
        assert match, f"{name}: no usage bar found"
        classes[name] = match.group(1).split()

    assert classes["device_row.html"] == classes["device_grid.html"]
    assert "disk-bar" not in classes["device_grid.html"]
