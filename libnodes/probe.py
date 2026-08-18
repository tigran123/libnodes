"""Reachability and free-space probes for the configured devices.

Requests never probe. A background task TCP-connects to the nodes that are due and writes
into a dict; handlers read the dict. That is what keeps `/devices/rows` — which the browser
polls every 10s — from turning six sleeping e-readers into a six-second page load.

**Two cadences, and only one of them is 10s.** The browser's poll is a hardcoded
`every 10s` in `devices.html`; it re-renders whatever is in the dict, however old. This
loop's own rate is `probe_interval` (10s) only for a node that answered. A node that did
not is backed off exponentially — 10, 20, 40, 80, 160 — up to `probe_backoff_max`, so it
is really contacted every five minutes and the 10s poll just re-paints a stale dot. Read
`probe_backoff_max` as "how long a recovery can go unnoticed", because that is the number
it sets: 310s measured against a live fleet, which is what `probe_backoff_watched` and
`note_interest` exist to cut to ~40s whenever a Devices page is actually open.

The two slow numbers compound, so a *red* dot is slower than it looks: `offline` needs
`sleeping_window` (1800s) to have passed since the node last answered, and a node down that
long is also pinned at the backoff ceiling. Amber `sleeping` is the first 30 minutes.
Losing a node is quick either way — while it is green the next probe is 10s out, so a
failure surfaces in ~22s including the poll.

Free space is a second, much slower probe (it costs a real ssh round trip), so it runs
on its own longer interval and is refreshed opportunistically after a transfer. It is
never awaited by this loop: a `df` is bounded at 15s and tried twice, and thirty seconds
per online node between one reachability sweep and the next is thirty seconds of every
dot being wrong.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import posixpath
import shlex
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Literal

from .config import DevicesStore, Settings
from .models import Device, parse_size
from .procs import reap

State = Literal["online", "sleeping", "offline", "unknown"]

#: How long a connection may be *completely* silent before ssh asks the far end whether
#: it is still there, and how many such questions may go unanswered before it gives up.
#: 60 x 3 = 180 s. Shared by `ssh_argv` and `build_argv` so the probe and the transfer
#: cannot disagree about the master they share. Reasoned about in ssh_argv's docstring.
SERVER_ALIVE_INTERVAL = 60
SERVER_ALIVE_COUNT_MAX = 3

log = logging.getLogger(__name__)

#: Bumped when the shape of probe.json changes. A file that does not match is dropped
#: rather than migrated -- it rebuilds itself within one probe interval, which is cheaper
#: than carrying a migration path for a cache.
_CACHE_VERSION = 1


def _only(row: dict, cls: type) -> dict:
    """The keys of `row` that `cls` actually declares, with everything else dropped.

    The cache is written by one version and read by another, so a field removed since the
    file was written would otherwise raise TypeError inside a restore that is supposed to
    be unable to fail. Missing keys need no handling -- every field has a default.
    """
    known = {f.name for f in fields(cls)}
    return {k: v for k, v in row.items() if k in known}


@dataclass(frozen=True)
class Reachability:
    state: State = "unknown"
    last_ok: float | None = None
    checked_at: float | None = None
    latency: float | None = None
    error: str | None = None
    #: Consecutive failures, which drive the backoff.
    failures: int = 0
    #: Earliest time the background loop should try again.
    next_probe_at: float = 0.0

    @property
    def online(self) -> bool:
        return self.state == "online"

    @property
    def dot_class(self) -> str:
        return {
            "online": "dot-ok",
            "sleeping": "dot-warn",
            "offline": "dot-err",
            "unknown": "dot-dim",
        }[self.state]


@dataclass(frozen=True)
class FreeSpace:
    total: int | None = None
    used: int | None = None
    free: int | None = None
    checked_at: float | None = None
    error: str | None = None

    @property
    def pct(self) -> float:
        if not self.total:
            return 0.0
        return 100.0 * (self.used or 0) / self.total


@dataclass(frozen=True)
class Battery:
    """What `cat <device.battery>` said, as a percentage, and whether it is on a charger.

    Separate from FreeSpace despite arriving down the same ssh: a device can answer one
    and not the other -- an unreadable sysfs node, or a `df` that toybox refused -- and
    folding them into one record would make either failure look like both.
    """

    percent: int | None = None
    #: Three states and not a bool, for two reasons. The row paints a different bolt for
    #: each of the first two -- amber while current is flowing, green while merely
    #: connected -- and `None` has to stay distinguishable from `"unplugged"`, or a node
    #: whose charger source did not answer would render as one we know to be on battery.
    #:
    #:   "charging"    drawing current
    #:   "plugged"     on the charger but not drawing: full, or paused
    #:   "unplugged"   on its own battery
    #:   None          not read, or the source said something we do not understand
    power: Literal["charging", "plugged", "unplugged"] | None = None
    checked_at: float | None = None
    error: str | None = None

    @property
    def known(self) -> bool:
        return self.percent is not None


@dataclass
class _Slot:
    reach: Reachability = field(default_factory=Reachability)
    space: FreeSpace = field(default_factory=FreeSpace)
    battery: Battery = field(default_factory=Battery)
    space_inflight: bool = False
    #: Re-read the space at the next opportunity whatever its age -- a transfer landed, or
    #: devices.yaml changed. A flag rather than a forged `checked_at`: that field dates the
    #: reading the row is showing, and the LAST SEEN column prints it, so nulling it to
    #: force a probe would make the column say "never" beside figures plainly on screen.
    space_stale: bool = False


def _describe(exc: BaseException) -> str:
    """Turn a connect failure into the short mono string the row shows inline."""
    if isinstance(exc, asyncio.TimeoutError):
        return "timed out"
    if isinstance(exc, OSError):
        if exc.errno == errno.EHOSTUNREACH:
            return "no route to host"
        if exc.errno == errno.ECONNREFUSED:
            return "connection refused"
        if exc.errno == errno.ENETUNREACH:
            return "network unreachable"
        if exc.errno == errno.ECONNRESET:
            return "connection reset"
        if exc.strerror:
            return exc.strerror.lower()
    text = str(exc).strip()
    return text.lower() if text else exc.__class__.__name__


class DeviceProbe:
    def __init__(self, settings: Settings, devices: DevicesStore) -> None:
        self.settings = settings
        self.devices = devices
        self._slots: dict[str, _Slot] = {}
        self._task: asyncio.Task | None = None
        self._rescan: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        #: Live `df` subprocesses. A space probe runs in a background task, so cancelling
        #: that task on shutdown abandons the ssh underneath it — see procs.reap.
        self._procs: set[asyncio.subprocess.Process] = set()
        self._listeners: set[asyncio.Queue] = set()
        #: When a Devices page last asked for the fleet. Drives the backoff ceiling; see
        #: note_interest. Zero means nobody has looked since startup.
        self._interest_at: float = 0.0
        #: The last failure `_loop` swallowed, so a permanent fault is logged once rather
        #: than every probe_interval for as long as the service runs.
        self._loop_error: str | None = None

    # --- accessors --------------------------------------------------------

    def _slot(self, device_id: str) -> _Slot:
        return self._slots.setdefault(device_id, _Slot())

    def status(self, device_id: str) -> Reachability:
        return self._slot(device_id).reach

    def space(self, device_id: str) -> FreeSpace:
        return self._slot(device_id).space

    def battery(self, device_id: str) -> Battery:
        return self._slot(device_id).battery

    @property
    def reachable_count(self) -> tuple[int, int]:
        devices = self.devices.config.devices
        online = sum(1 for d in devices if self.status(d.id).online)
        return online, len(devices)

    @property
    def last_scan(self) -> float | None:
        stamps = [
            s.reach.checked_at for s in self._slots.values() if s.reach.checked_at
        ]
        return max(stamps) if stamps else None

    # --- reachability -----------------------------------------------------

    async def probe(self, device: Device) -> Reachability:
        """One TCP connect. Cheap enough to run against every device each tick."""
        slot = self._slot(device.id)
        started = time.time()
        try:
            fut = asyncio.open_connection(device.host, device.effective_port)
            reader, writer = await asyncio.wait_for(
                fut, timeout=self.settings.probe_timeout
            )
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass
            now = time.time()
            slot.reach = Reachability(
                state="online",
                last_ok=now,
                checked_at=now,
                latency=now - started,
                error=None,
                failures=0,
                next_probe_at=now + self.settings.probe_interval,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            now = time.time()
            previous = slot.reach
            # A node that answered recently is asleep, not gone. Termux sshd stops with
            # the screen and Kobos suspend aggressively; the design distinguishes the
            # two because only one of them is worth a wake-on-LAN.
            recent = (
                previous.last_ok is not None
                and now - previous.last_ok < self.settings.sleeping_window
            )
            failures = previous.failures + 1
            slot.reach = Reachability(
                state="sleeping" if recent else "offline",
                last_ok=previous.last_ok,
                checked_at=now,
                latency=None,
                error=_describe(exc),
                failures=failures,
                next_probe_at=now + self._backoff(failures),
            )
        return slot.reach

    def note_interest(self) -> None:
        """Record that a Devices page asked for the fleet.

        A stamp, not a probe — this must stay I/O-free, because it runs inside a request
        and "requests never probe a device" is what keeps six sleeping e-readers from
        becoming a six-second page load. All it does is tell the background loop that a
        slow retry would now be seen by somebody, which `_backoff` turns into a shorter
        ceiling.
        """
        self._interest_at = time.time()

    @property
    def watched(self) -> bool:
        return time.time() - self._interest_at < self.settings.watch_window

    def _backoff(self, failures: int) -> float:
        """Exponential, capped. A node dead for an hour is not news every 10 seconds —
        unless a Devices page is open on it, in which case that cap *is* the complaint:
        it is the whole reason a device that came back stays red for five minutes."""
        delay = self.settings.probe_interval * (2 ** min(failures - 1, 8))
        ceiling = (
            self.settings.probe_backoff_watched
            if self.watched
            else self.settings.probe_backoff_max
        )
        return min(delay, ceiling)

    def due(self, device: Device, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        reach = self._slot(device.id).reach
        if reach.next_probe_at <= now:
            return True
        # `next_probe_at` was frozen at failure time, under whichever ceiling was in force
        # then. A page opening now would otherwise have to wait out an appointment made
        # while nobody was watching -- which is exactly the case being fixed -- so re-judge
        # the wait against the ceiling that applies at this moment. Additive: this can only
        # ever make a device more due, never less.
        if reach.checked_at is not None and reach.failures:
            return now - reach.checked_at >= self._backoff(reach.failures)
        return False

    async def probe_all(self, force: bool = False) -> None:
        """Probe every node that is due. Concurrent, so wall time is one timeout.

        `force` ignores the backoff — that is what the Rescan button means.
        """
        devices = self.devices.config.devices
        if not devices:
            return
        wanted = devices if force else [d for d in devices if self.due(d)]
        if not wanted:
            return
        before = {d.id: self.status(d.id).state for d in wanted}
        await asyncio.gather(
            *(self.probe(d) for d in wanted), return_exceptions=True
        )
        flipped = [d.id for d in wanted if before.get(d.id) != self.status(d.id).state]
        if flipped:
            self._notify(flipped)

    def rescan_soon(self, force: bool = True) -> None:
        """Kick a probe sweep without making the caller wait for it.

        Requests must never block on device I/O — a set of unreachable devices would
        otherwise make the UI as slow as the slowest timeout.
        """
        if self._rescan is not None and not self._rescan.done():
            return
        self._rescan = asyncio.create_task(self.probe_all(force=force))

    def probe_space_soon(self, device: Device, force: bool = False) -> None:
        """Same, for the `ssh … df` probe, which is far slower than a TCP connect."""
        task = asyncio.create_task(self.probe_space(device, force=force))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # --- free space -------------------------------------------------------

    async def probe_space(self, device: Device, force: bool = False) -> FreeSpace:
        """`ssh … df -Pk <target>`. Slow, so cached and never awaited by a page render."""
        slot = self._slot(device.id)
        now = time.time()
        fresh = (
            slot.space.checked_at is not None
            and not slot.space_stale
            and now - slot.space.checked_at < self.settings.freespace_interval
        )
        if (fresh and not force) or slot.space_inflight:
            return slot.space
        if not self.status(device.id).online:
            return slot.space

        slot.space_inflight = True
        # Cleared here, where the probe commits to the ssh, rather than beside each of the
        # four places below that store a reading: every outcome -- parsed, unparsed, error,
        # timeout -- writes a fresh `checked_at`, so age alone is enough to schedule the
        # next one, and one site cannot fall out of step with the others.
        slot.space_stale = False
        try:
            target = shlex.quote(device.target)
            # `-Pk` is the portable-output form on GNU coreutils, but Android's toybox df
            # rejects both flags outright (exit 1) — and Termux is a primary target
            # class. So try the strict form, then plain `df`, and judge by whether the
            # output parses rather than by the exit code.
            parsed = None
            last_error = "df failed"
            for command in (
                _readings_script(device, f"df -Pk {target}"),
                _readings_script(device, f"df {target}"),
            ):
                proc = await asyncio.create_subprocess_exec(
                    *ssh_argv(device, self.settings),
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._procs.add(proc)
                try:
                    out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
                except asyncio.TimeoutError:
                    # kill() alone only asks; the transport stays open until something
                    # waits for the child. See procs.reap.
                    await reap([proc])
                    slot.space = FreeSpace(checked_at=time.time(), error="df timed out")
                    return slot.space
                finally:
                    # Deregister only a child that has actually exited. On shutdown the
                    # await above raises CancelledError, and an unconditional discard
                    # here hands the proc back a moment before stop() reaps
                    # self._procs -- so the one process that needs reaping is the one
                    # missing from the set. Its transport then waits on an EOF nobody
                    # will read and is collected after the loop has closed, which is the
                    # nameless `RuntimeError: Event loop is closed` procs.py exists to
                    # prevent. Reproduced at roughly one run in three by exercising ~18
                    # app lifecycles in one suite.
                    if proc.returncode is not None:
                        self._procs.discard(proc)

                stderr_tail = err.decode(errors="replace").strip().splitlines()
                if stderr_tail:
                    last_error = stderr_tail[-1]
                text = out.decode(errors="replace")
                # Recorded on every attempt, not only the one whose df parsed: a toybox
                # node fails the first command and still reads its battery on it, and a
                # node whose df never parses should not lose its battery figure too.
                if device.battery or device.battery_cmd:
                    self.adopt_battery(
                        device.id,
                        _section(text, "battery"),
                        _section(text, "power"),
                    )
                parsed = _parse_df(_section(text, "df") or text)
                if parsed is not None:
                    break

            if parsed is None:
                # Fall back to the declared capacity so the bar still renders.
                slot.space = FreeSpace(
                    total=device.capacity_bytes,
                    checked_at=time.time(),
                    error=last_error,
                )
            else:
                total, used, free = parsed
                slot.space = FreeSpace(
                    total=total, used=used, free=free, checked_at=time.time()
                )
        except (OSError, ValueError) as exc:
            slot.space = FreeSpace(checked_at=time.time(), error=_describe(exc))
        finally:
            slot.space_inflight = False
        return slot.space

    def adopt_battery(self, device_id: str, text: str, power_text: str = "") -> None:
        """Store what the battery source said. Never raises — a device that cannot answer
        this must not cost us the `df` that came back on the same ssh.

        Public and named to match `adopt_space`, because the connection test reads the
        same two things over an ssh it has already opened and there is no reason for the
        row behind it to keep an older figure for either.

        `power_text` is the `# power` section, which only the file form produces: a
        `battery_cmd` node's own JSON already carries `plugged` and `status`, so it falls
        back to `text`. Falling back on emptiness rather than on a failed parse matters —
        a `status` file that errored is not empty, and must not send us looking for a
        charger in a bare integer.
        """
        percent = _parse_battery(text)
        # The charge state is never carried forward, in either branch below. A percentage
        # degrades gracefully with age -- it is a level, and levels move slowly -- but a
        # bolt is a claim about *now*, and a stale one says a device is on a charger it may
        # have been unplugged from minutes ago. Not read this time means no bolt.
        power = _parse_power(power_text or text)
        if percent is None:
            # Keep the last known figure rather than blanking the bar on one bad read;
            # the error is what says the reading is no longer being refreshed.
            previous = self._slot(device_id).battery
            self._slot(device_id).battery = Battery(
                percent=previous.percent,
                power=power,
                checked_at=time.time(),
                error=_battery_error(text),
            )
            return
        self._slot(device_id).battery = Battery(
            percent=percent, power=power, checked_at=time.time()
        )

    def invalidate_space(self, device_id: str) -> None:
        """Force the next space probe, e.g. right after a transfer landed.

        The figures are kept — the cell must not blink empty for a tick — and so is their
        `checked_at`, which is what dates them in the LAST SEEN column. Only the schedule
        is touched.
        """
        self._slot(device_id).space_stale = True

    def refresh_all(self) -> None:
        """Drop every cached reading, so the next sweep re-reads the whole fleet.

        For a devices.yaml edit. The config hot-reloads within a tick, but the readings
        are cached quite independently of it, so adding a `battery:` line changed what we
        would ask for while `freespace_interval` kept us from asking for up to five more
        minutes — the new column sitting empty with nothing on the page to say why. Note
        that this is the flat 5-minute `df` cache and not the reachability backoff, two
        different settings that both happen to default to 300s.

        Invalidates and lets `_loop` pick it up on its next tick rather than probing from
        here: the loop already spawns one task per device and `space_inflight` keeps them
        from overlapping, whereas an editor that saves twice in a second — write, chmod,
        rename is one save — would otherwise start two sweeps over the same devices.
        Reachability is kicked directly because it is a 2s connect, and `rescan_soon`
        already refuses to start a second sweep while one is running.
        """
        for device in self.devices.config.devices:
            self.invalidate_space(device.id)
        self.rescan_soon(force=True)

    def adopt_space(self, device_id: str, text: str) -> bool:
        """Take a `df` reading somebody else already paid for. True if it parsed.

        The connection test runs the same `df -Pk <target>` this probe would, over an ssh
        it has already opened, so asking the device again to learn what it has just been
        told is a round trip for nothing. Worse, without this the row the test swaps out
        of band renders the *previous* poll's figure while the dialog above it shows the
        number just measured — the two disagreeing on screen at the same moment.
        """
        parsed = _parse_df(text)
        if parsed is None:
            return False
        total, used, free = parsed
        slot = self._slot(device_id)
        slot.space = FreeSpace(
            total=total, used=used, free=free, checked_at=time.time()
        )
        slot.space_stale = False
        return True

    # --- change notification ---------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        self._listeners.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._listeners.discard(queue)

    def _notify(self, device_ids: list[str]) -> None:
        for queue in list(self._listeners):
            try:
                queue.put_nowait(device_ids)
            except asyncio.QueueFull:
                pass

    # --- lifecycle --------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                await self.probe_all()
                for device in self.devices.config.devices:
                    if self.status(device.id).online and self._space_stale(device.id):
                        # Spawned, never awaited. A `df` is bounded at 15s and tried
                        # twice, so awaiting it here put up to 30s per online node
                        # between one reachability sweep and the next -- 30s in which
                        # every dot on the page is whatever it was before.
                        self.probe_space_soon(device)
                    # Offline nodes are skipped entirely: probe_space already returns
                    # early for them, and there is nothing to ask a dead host.
                self._loop_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a probe must never kill the loop
                # Swallowed, but not silently. A body that raises every time leaves the
                # whole fleet grey and /healthz reporting `online: 0`, which is exactly
                # what a genuinely dark fleet looks like -- there is no way to tell the
                # two apart from outside the process. Logged on the first occurrence and
                # again only when the fault changes, so a permanent one does not write a
                # line every probe_interval for as long as the service runs. No handler is
                # configured anywhere in the app, so this reaches the journal through
                # logging.lastResort; see the same posture in main.py.
                current = f"{type(exc).__name__}: {exc}"
                if current != self._loop_error:
                    self._loop_error = current
                    log.warning("device probe sweep failed: %s", current)
            await asyncio.sleep(self.settings.probe_interval)

    def _space_stale(self, device_id: str) -> bool:
        """Whether a `df` is worth spawning. `probe_space` returns early on a fresh
        reading anyway, so this only avoids creating a task per device per tick to do
        nothing."""
        slot = self._slot(device_id)
        checked = slot.space.checked_at
        return (
            slot.space_stale
            or checked is None
            or time.time() - checked >= self.settings.freespace_interval
        )

    def start(self) -> None:
        self.load_cache()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="device-probe")

    def load_cache(self) -> None:
        """Restore last session's readings, with the ages they actually have.

        `checked_at` comes back untouched, so LAST SEEN says "4h ago" rather than
        pretending to be fresh — and every staleness test already in this file then treats
        the reading as due, so a restored figure schedules its own replacement instead of
        suppressing one. That is the whole trick: nothing downstream needs to know these
        came off disk.

        `reach` is restored only in part, and the omissions are the point. `last_ok` is a
        historical fact — when this node last answered — and it is what separates amber
        `sleeping` from red `offline`, so without it every unreachable node reads as
        half-an-hour-dead the moment the service comes back. `state` is a *measurement* and
        must be taken now, so it is not restored; nor are `checked_at` and `next_probe_at`,
        which would have the first sweep honour a backoff appointment made last session.

        Never raises. A cache is a convenience, and a corrupt one must cost a cold fleet
        rather than a start-up.
        """
        path = self.settings.probe_cache
        try:
            blob = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(blob, dict) or blob.get("version") != _CACHE_VERSION:
            # Dropped rather than migrated: the file rebuilds itself within one probe
            # interval, which is a cheaper price than a migration path for a cache.
            return
        for device_id, row in (blob.get("devices") or {}).items():
            if not isinstance(row, dict):
                continue
            slot = self._slot(str(device_id))
            space = row.get("space")
            if isinstance(space, dict):
                slot.space = FreeSpace(**_only(space, FreeSpace))
            battery = row.get("battery")
            if isinstance(battery, dict):
                slot.battery = Battery(**_only(battery, Battery))
            last_ok = row.get("last_ok")
            if isinstance(last_ok, (int, float)):
                slot.reach = Reachability(last_ok=float(last_ok))

    def save_cache(self) -> None:
        """Write the readings out so a restart does not blank the fleet.

        At shutdown and nowhere else. Nothing reads this file while the process runs, so a
        periodic flush would buy durability against an *unclean* exit only, and it would
        cost a write every time a node answered — every 10s across six nodes, for data
        nobody is going to read. A deploy is `systemctl restart`, which is SIGTERM, which
        runs the lifespan shutdown, which calls this. That is the case that motivated it:
        a deploy used to blank every node that happened to be asleep at that moment, and on
        this fleet the Kobo can be asleep for days.

        Temp file and rename, so a kill part-way through leaves the previous cache intact
        rather than a half-written one that the loader would then have to distrust.

        Never raises. This must not be able to fail a shutdown that still has real rsync
        subprocesses to reap.
        """
        path = self.settings.probe_cache
        devices = {}
        for device_id, slot in self._slots.items():
            row: dict = {}
            if slot.space.checked_at is not None:
                row["space"] = asdict(slot.space)
            if slot.battery.checked_at is not None:
                row["battery"] = asdict(slot.battery)
            if slot.reach.last_ok is not None:
                row["last_ok"] = slot.reach.last_ok
            if row:
                devices[device_id] = row
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps({"version": _CACHE_VERSION, "devices": devices}, indent=1)
            )
            tmp.replace(path)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("could not write %s: %s", path, exc)

    async def stop(self) -> None:
        for task in [self._task, self._rescan, *self._background]:
            if task is not None:
                task.cancel()
        for task in [self._task, self._rescan, *self._background]:
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        # Between the cancels and the reap, deliberately. After the cancels, because no
        # task can still be writing a reading, so what lands on disk is final. Before the
        # reap, because reaping waits on real rsync subprocesses and a shutdown that runs
        # long -- or gets killed for running long -- must not be what loses the cache.
        self.save_cache()
        # After the tasks, not before: reap() drains the pipes, and a cancelled task is
        # one that has stopped reading them. See procs.reap.
        await reap(list(self._procs))
        self._procs.clear()
        self._task = None
        self._rescan = None
        self._background.clear()


def ssh_argv(device: Device, settings: Settings, timeout: int = 10) -> list[str]:
    """The ssh command prefix shared by probes, tests and remote listings.

    Built as an argv list, never a shell string. BatchMode guarantees a missing key
    fails immediately instead of blocking the event loop on a password prompt.

    The keepalives are about ssh multiplexing, which the Pi's ~/.ssh/config turns on
    (`ControlMaster auto`, `ControlPersist 3600`) and which this inherits, not passing
    `-F none`. That is worth having: measured against the fleet, a fresh handshake to a
    phone costs 680-850 ms and a multiplexed session 140-355 ms. The cost is that a
    master outlives the connection under it — a phone that sleeps leaves one wedged, and
    the next probe through it fails with `mux_client_request_session: read from master
    failed: Broken pipe` rather than reconnecting.

    ServerAlive is what makes the master notice. It only fires after `Interval` seconds
    with *no data received at all*, so an active transfer resets it continuously and it
    cannot kill a busy connection; 60 x 3 = 180 s of true silence. Under the 300 s
    freespace_interval on purpose, so a woken device costs at most one failed reading
    rather than three. Set explicitly rather than left to Debian's BatchMode default of
    300 (x3 = 900 s, i.e. a quarter of an hour of stale storage and battery), and kept
    well clear of values tight enough to drop a working link: at 5 x 1, five seconds of
    ordinary jitter — rsync checksumming a large file, a stalled FAT write — is a
    disconnect.
    """
    argv = ["ssh", "-p", str(device.effective_port)]
    if device.identity:
        argv += ["-i", str(device.identity)]
    argv += [
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ServerAliveInterval={SERVER_ALIVE_INTERVAL}",
        "-o",
        f"ServerAliveCountMax={SERVER_ALIVE_COUNT_MAX}",
    ]
    extra = device.effective_ssh_options
    if extra:
        argv += shlex.split(extra)
    argv.append(f"{device.effective_user}@{device.host}")
    return argv


def battery_command(device: Device) -> str | None:
    """The shell fragment that reads this device's charge, or None if it declares none.

    One definition, because the quoting rule differs between the two forms and a second
    copy of it would eventually get one wrong. `battery` is a path and is quoted as one.
    `battery_cmd` is a command line and must *not* be: it is meant to be able to be a
    pipeline, and quoting it would run the whole string as the name of one program.

    That second case is a shell string built from config, which is exactly what
    `build_argv` and `ssh_argv` refuse to do. The difference is that this value *is* a
    command by declaration — written by whoever already has ssh to the device — rather
    than a filename that ends up in one.

    Shared by the background probe and the connection test, so the two cannot drift.
    """
    if device.battery:
        return f"cat {shlex.quote(device.battery)} 2>&1"
    if device.battery_cmd:
        return f"{device.battery_cmd} 2>&1"
    return None


def charging_command(device: Device) -> str | None:
    """The shell fragment that reads whether this device is on a charger, or None.

    Only the file form needs one. A `battery_cmd` node runs `termux-api BatteryStatus`,
    whose JSON already carries `plugged` and `status` beside the percentage — asking twice
    would be a second invocation for something we have already been told.

    For the file form the answer is the `status` file in the same directory as the
    declared `capacity`. That is a narrower guess than the one `Device.battery` exists to
    avoid: the sysfs power-supply ABI fixes both names *within* one supply directory, so
    the only thing being assumed is that whoever named `capacity` named a real supply. And
    unlike a wrong percentage, a `status` that is not there fails the `cat` and draws
    nothing — a blank, not a confident wrong claim.

    Deliberately *not* the `online` file of some other supply, which looks like the more
    direct question and is not answerable. Measured on lg (Android 6): `charger_controller`
    reports `status: Charging` and `online: 1` permanently, while the phone is plainly
    unplugged — `usb/present: 0`, battery `Discharging`, every other supply `online: 0`.
    Its `usb` supply is also typed `Unknown` rather than `USB`, so "the non-battery supply
    that is online" picks the liar and skips the truth on the same device. `status` was
    correct on both nodes that were awake to ask.

    A device may override the derivation with `charging:`, and one has to: the Nexus 10
    reads its charge from a fuel gauge that exposes no `status`, while its charger is a
    separate supply two directories away. That is also why the fallback is not the sign of
    `POWER_SUPPLY_CURRENT_NOW`, which looks like a general answer and is not -- measured on
    this fleet, lg and bk both report a *positive* current while `STATUS=Discharging`, the
    opposite convention to the Nexus 10's. The sign is a per-driver accident; a declared
    path is a fact.

    Shared by the background probe and the connection test, for the same reason
    `battery_command` is: one definition, so the two cannot drift.
    """
    if device.charging:
        return f"cat {shlex.quote(device.charging)} 2>&1"
    if not device.battery:
        return None
    directory = posixpath.dirname(device.battery)
    if not directory:
        # A bare filename names no supply directory, so there is no sibling to derive.
        return None
    status = posixpath.join(directory, "status")
    return f"cat {shlex.quote(status)} 2>&1"


def _readings_script(device: Device, df_command: str) -> str:
    """One shell line that reads everything an ssh round trip can get us at once.

    The battery reading rides along with `df` rather than opening a second connection: on
    a sleeping Termux node the connection *is* the cost, and two probes on their own
    schedules would also drift out of step in the row that shows both. Marked sections
    rather than positional parsing, because `df` output is one line on some devices and
    two on others — see `_parse_df`.
    """
    read = battery_command(device)
    if read is None:
        return df_command
    script = f'echo "# df"; {df_command}; echo "# battery"; {read}'
    charger = charging_command(device)
    if charger is not None:
        script += f'; echo "# power"; {charger}'
    return script


def _section(text: str, name: str) -> str:
    """The `# <name>` block of a marked transcript, up to the next marker.

    Returns "" when the marker is absent, which is also what an unmarked single-command
    transcript yields — callers fall back to the whole text for that case.
    """
    lines = text.splitlines()
    start = next(
        (i + 1 for i, ln in enumerate(lines) if ln.strip() == f"# {name}"), None
    )
    if start is None:
        return ""
    end = next(
        (i for i in range(start, len(lines)) if lines[i].startswith("# ")), len(lines)
    )
    return "\n".join(lines[start:end])


#: JSON keys that mean "percent charged", most specific first. termux-api says
#: `percentage`; upower and several sysfs-scraping wrappers say `capacity`; Android's own
#: battery intent calls it `level`. Matched exactly and case-insensitively rather than by
#: substring, so `percentage_design` or `level_raw` cannot answer for the charge.
_BATTERY_KEYS = ("percentage", "capacity", "level", "battery_level")


def _parse_battery(text: str) -> int | None:
    """A battery reading as a percentage, or None if it did not read like one.

    Two shapes, because the source is either a file or a program. A sysfs file is a bare
    integer and nothing else. `termux-api BatteryStatus` prints a JSON object, which is
    what Android 12 forces: /sys/class/power_supply is unreadable from Termux there, so
    there is nothing to cat.

    Anything else fails rather than being pattern-matched out of a longer message. A
    `cat` of a missing node prints "No such file or directory (2)", and pulling the 2 out
    of that would report 2% charge -- a plausible wrong number is worse than a blank.
    """
    stripped = text.strip()
    if not stripped:
        return None

    first = stripped.splitlines()[0].strip()
    if first.isdigit():
        return _as_percent(int(first))

    if not stripped.startswith("{"):
        return None
    try:
        blob = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(blob, dict):
        return None
    lowered = {str(k).lower(): v for k, v in blob.items()}
    for key in _BATTERY_KEYS:
        if key in lowered:
            value = lowered[key]
            # bool is an int in Python, and `{"charging": true}` is not 1% charge.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            found = _as_percent(value)
            if found is not None:
                return found
    return None


#: What a sysfs `status` file says, mapped to what the row draws. The kernel's set is
#: fixed (`power_supply_sysfs.c`): Unknown, Charging, Discharging, Not charging, Full.
#: Android's battery intent uses the same words in upper snake case, so one table serves
#: both sources. `Full` and `Not charging` are both "on the charger, not taking" -- the
#: second is a charger that has paused, usually on temperature -- and neither is reachable
#: without a charger attached.
#:
#: `Unknown` is deliberately absent: it means the driver does not know, which is not a
#: fact about the charger and must not be drawn as one.
_POWER_WORDS = {
    "charging": "charging",
    "full": "plugged",
    "not charging": "plugged",
    "not_charging": "plugged",
    "discharging": "unplugged",
}


def _parse_power(text: str) -> str | None:
    """Whether the device is on a charger: "charging", "plugged", "unplugged" or None.

    Two shapes again, and for the same reason as `_parse_battery` -- the source is either
    a file or a program -- but the two carry different amounts of information. A sysfs
    `status` file is one word. `termux-api BatteryStatus` prints both `plugged`, which is
    the authority on whether a charger is attached, and `status`, which is the authority on
    whether current is flowing; a payload holding only one of them falls back to it alone.

    Anything unrecognised is None rather than a guess. A failed `cat` lands here, and so
    does `Unknown` from a driver that does not know -- in both cases the honest answer is
    to draw no bolt, which is also what an unplugged device gets. That is why the caller
    keeps `None` and `"unplugged"` apart: only the tooltip can tell them apart, and it does.
    """
    stripped = text.strip()
    if not stripped:
        return None

    if not stripped.startswith("{"):
        # A status file holds one word and nothing else. Matched whole, so the error text
        # of a failed `cat` -- which may well contain "charging" as part of the path it
        # could not open -- cannot answer for the charger.
        return _POWER_WORDS.get(stripped.splitlines()[0].strip().lower())

    try:
        blob = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(blob, dict):
        return None
    lowered = {str(k).lower(): v for k, v in blob.items()}

    status = lowered.get("status")
    flowing = _POWER_WORDS.get(str(status).strip().lower()) if status else None

    plugged = lowered.get("plugged")
    if isinstance(plugged, str) and plugged.strip():
        word = plugged.strip().upper()
        if word == "UNPLUGGED":
            return "unplugged"
        if word.startswith("PLUGGED"):
            # Attached for certain; `status` only decides which of the two bolts. An
            # UNKNOWN or missing status on an attached charger is still attached.
            return "charging" if flowing == "charging" else "plugged"
        return None

    return flowing


def _battery_error(text: str) -> str:
    """Why a reading did not parse, in the few words a tooltip has room for.

    A failed `cat` puts its complaint on the last line, which is the useful one. A JSON
    object that simply lacks a charge key has `}` there instead, which says nothing — so
    that case is named rather than quoted, with the keys we did see.
    """
    stripped = text.strip()
    if not stripped:
        return "no output"
    if stripped.startswith("{"):
        try:
            blob = json.loads(stripped)
        except ValueError:
            return "output is not valid JSON"
        if isinstance(blob, dict):
            keys = ", ".join(sorted(str(k) for k in blob)) or "nothing"
            return f"no charge key in JSON (saw: {keys})"
        return "JSON is not an object"
    return stripped.splitlines()[-1].strip()


def _as_percent(value: int | float) -> int | None:
    """A number that is a percentage, rounded, or None. Rounded because a JSON source may
    report a float; out-of-range is rejected rather than clamped, since a figure outside
    0..100 means the key was not the charge after all."""
    if not 0 <= value <= 100:
        return None
    return int(round(value))


def _df_field(token: str) -> int | None:
    """One df size field -> bytes.

    Two dialects reach us and the column positions happen to agree, so only the value
    format differs:

        GNU `df -Pk`  ``/dev/mmcblk0p3  30408704  7969472  22439232  27% /mnt/onboard``
        toybox `df`   ``/…/sd/Books      466.35G   302.90G   163.46G  32768``

    A bare integer is a 1K block count; anything carrying a unit suffix is absolute.
    """
    token = token.strip().rstrip("%")
    if not token:
        return None
    if token.isdigit():
        return int(token) * 1024
    return parse_size(token)


def _parse_df(text: str) -> tuple[int, int, int] | None:
    """Read df output as (total, used, free) bytes, or None if it makes no sense."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    for line in reversed(lines[1:]):
        parts = line.split()
        if not parts:
            continue
        if not parts[0].isdigit() and len(parts) >= 4:
            fields = parts[1:4]          # normal record: name, total, used, free
        elif parts[0].isdigit() and len(parts) >= 3:
            # Without -P a long device name wraps, leaving the numbers on their own
            # line with no Filesystem column to skip past.
            fields = parts[0:3]
        else:
            continue
        total, used, free = (_df_field(p) for p in fields)
        if total is None or used is None or free is None:
            continue
        if total <= 0:
            continue
        return (total, used, free)
    return None


__all__ = [
    "Battery",
    "DeviceProbe",
    "FreeSpace",
    "Reachability",
    "State",
    "ssh_argv",
    "parse_size",
]
