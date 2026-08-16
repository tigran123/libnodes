"""Reachability probing must never make the UI wait on a dead device."""

from __future__ import annotations

import asyncio
import time

import pytest

from libnodes.probe import DeviceProbe, Reachability, _parse_df


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
