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
import logging
import shlex
import time
from dataclasses import dataclass, field, replace
from typing import Literal

from .config import DevicesStore, Settings
from .models import Device, parse_size
from .procs import reap

State = Literal["online", "sleeping", "offline", "unknown"]

log = logging.getLogger(__name__)


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
    """What `cat <device.battery>` said, as a percentage.

    Separate from FreeSpace despite arriving down the same ssh: a device can answer one
    and not the other -- an unreadable sysfs node, or a `df` that toybox refused -- and
    folding them into one record would make either failure look like both.
    """

    percent: int | None = None
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
            and now - slot.space.checked_at < self.settings.freespace_interval
        )
        if (fresh and not force) or slot.space_inflight:
            return slot.space
        if not self.status(device.id).online:
            return slot.space

        slot.space_inflight = True
        try:
            target = shlex.quote(device.target)
            # `-Pk` is the portable-output form on GNU coreutils, but Android's toybox df
            # rejects both flags outright (exit 1) — and Termux is a primary target
            # class. So try the strict form, then plain `df`, and judge by whether the
            # output parses rather than by the exit code.
            parsed = None
            last_error = "df failed"
            for command in (
                _readings_script(f"df -Pk {target}", device.battery),
                _readings_script(f"df {target}", device.battery),
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
                if device.battery:
                    self._record_battery(device.id, _section(text, "battery"))
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

    def _record_battery(self, device_id: str, text: str) -> None:
        """Store what the battery file said. Never raises — a device that cannot answer
        this must not cost us the `df` that came back on the same ssh."""
        percent = _parse_battery(text)
        if percent is None:
            # Keep the last known figure rather than blanking the bar on one bad read;
            # the error is what says the reading is no longer being refreshed.
            previous = self._slot(device_id).battery
            self._slot(device_id).battery = Battery(
                percent=previous.percent,
                checked_at=time.time(),
                error=text.strip().splitlines()[-1] if text.strip() else "unreadable",
            )
            return
        self._slot(device_id).battery = Battery(
            percent=percent, checked_at=time.time()
        )

    def invalidate_space(self, device_id: str) -> None:
        """Force the next space probe, e.g. right after a transfer landed."""
        slot = self._slot(device_id)
        slot.space = replace(slot.space, checked_at=None)

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
        self._slot(device_id).space = FreeSpace(
            total=total, used=used, free=free, checked_at=time.time()
        )
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
        checked = self._slot(device_id).space.checked_at
        return (
            checked is None
            or time.time() - checked >= self.settings.freespace_interval
        )

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="device-probe")

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
    ]
    extra = device.effective_ssh_options
    if extra:
        argv += shlex.split(extra)
    argv.append(f"{device.effective_user}@{device.host}")
    return argv


def _readings_script(df_command: str, battery: str | None) -> str:
    """One shell line that reads everything an ssh round trip can get us at once.

    The battery file rides along with `df` rather than opening a second connection: on a
    sleeping Termux node the connection *is* the cost, and two probes on their own
    schedules would also drift out of step in the row that shows both. Marked sections
    rather than positional parsing, because `df` output is one line on some devices and
    two on others — see `_parse_df`.
    """
    if not battery:
        return df_command
    return (
        f'echo "# df"; {df_command}; '
        f'echo "# battery"; cat {shlex.quote(battery)} 2>&1'
    )


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


def _parse_battery(text: str) -> int | None:
    """A battery sysfs file as a percentage, or None if it did not read like one.

    The file is conventionally a bare integer and nothing else, so anything that is not
    one is treated as a failure rather than pattern-matched out of a longer message: a
    `cat` of a missing node prints an error, and reading `2` out of "No such file or
    directory (2)" would put a confident wrong number on the row.
    """
    stripped = text.strip()
    if not stripped:
        return None
    first = stripped.splitlines()[0].strip()
    if not first.isdigit():
        return None
    value = int(first)
    if not 0 <= value <= 100:
        return None
    return value


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
