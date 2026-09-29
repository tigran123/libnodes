"""Reachability and free-space probes for the configured devices.

Requests never probe: a background task TCP-connects to the nodes that are due and writes a
dict that handlers read, so six sleeping e-readers cannot become a six-second page load.

Two cadences. The browser re-renders the dict every 10 s, however old it is; this loop
contacts a failing node on an exponential backoff up to `probe_backoff_max`, which is
therefore how long a recovery can go unnoticed (310 s measured) -- and `note_interest` cuts
it to `probe_backoff_watched` while a Devices page polls. Red `offline` needs
`sleeping_window` since the last answer; amber `sleeping` is before that. Losing a node
surfaces in ~22 s.

Free space and battery are one slower ssh (`_readings_script`), on their own interval and
refreshed after a transfer, and never awaited by the sweep.
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

#: ssh keepalives: 60 s of total silence, three times. See `ssh_base`.
SERVER_ALIVE_INTERVAL = 60
SERVER_ALIVE_COUNT_MAX = 3

log = logging.getLogger(__name__)

#: Bumped when probe.json's shape changes; a mismatched file is dropped, not migrated.
_CACHE_VERSION = 1


def _only(row: dict, cls: type) -> dict:
    """`row` without keys `cls` no longer declares, so an older cache still restores."""
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
    """The charge and whether a charger is attached. Separate from FreeSpace though it
    arrives on the same ssh, because either can fail alone."""

    percent: int | None = None
    #: "charging" (drawing current), "plugged" (attached, not drawing), "unplugged", or
    #: None (unread or not understood) -- kept apart from "unplugged" so the tooltip can say
    #: which. Whether a bolt is drawn is `DeviceView.bolt_class`'s decision.
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
    #: Re-read at the next chance whatever its age. A flag, not a nulled `checked_at`,
    #: which dates the figures on screen in LAST SEEN.
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
            # A node that answered recently is asleep, not gone.
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
        """Record that a Devices page asked for the fleet. A stamp, never I/O: it runs
        inside a request. `_backoff` turns it into a shorter ceiling."""
        self._interest_at = time.time()

    @property
    def watched(self) -> bool:
        return time.time() - self._interest_at < self.settings.watch_window

    def _backoff(self, failures: int) -> float:
        """Exponential, capped -- lower while somebody is watching."""
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
        # Re-judged against the ceiling in force now, or a page opening would wait out an
        # appointment made while nobody watched. Only ever makes a device more due.
        if reach.checked_at is not None and reach.failures:
            return now - reach.checked_at >= self._backoff(reach.failures)
        return False

    async def probe_all(self, force: bool = False) -> None:
        """Probe every node that is due, concurrently; `force` (Rescan) ignores the backoff.
        State changes go to subscribers, which is how a deferred job hears of its node."""
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
        """Kick a sweep without waiting for it."""
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
        # places below that store a reading: every outcome -- parsed, unparsed, error,
        # timeout -- writes a fresh `checked_at`, so age alone is enough to schedule the
        # next one, and one site cannot fall out of step with the others.
        slot.space_stale = False
        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_argv(device, self.settings),
                _readings_script(device),
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
                # Deregister only a child that has actually exited. On shutdown the await
                # above raises CancelledError, and an unconditional discard here hands the
                # proc back a moment before stop() reaps self._procs -- so the one process
                # that needs reaping is the one missing from the set. Its transport then
                # waits on an EOF nobody will read and is collected after the loop has
                # closed, which is the nameless `RuntimeError: Event loop is closed`
                # procs.py exists to prevent. Reproduced at roughly one run in three by
                # exercising ~18 app lifecycles in one suite.
                if proc.returncode is not None:
                    self._procs.discard(proc)

            text = out.decode(errors="replace")
            if device.battery or device.battery_cmd:
                self.adopt_battery(
                    device.id, _section(text, "battery"), _section(text, "power")
                )
            df = _section(text, "df")
            parsed = _parse_df(df)
            if parsed is None:
                # The df's own complaint when it ran, ssh's when it did not.
                said = df.strip().splitlines() or err.decode(errors="replace").strip().splitlines()
                # Fall back to the declared capacity so the bar still renders.
                slot.space = FreeSpace(
                    total=device.capacity_bytes,
                    checked_at=time.time(),
                    error=said[-1] if said else "df failed",
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
        """Store what the battery source said; never raises. Public, because the Test
        button reads the same things on its own ssh.

        `power_text` is the `# power` section; a `battery_cmd`'s JSON carries its own
        charger, so an *empty* one falls back to `text` (an errored `status` is not empty).
        """
        percent = _parse_battery(text)
        # The charge state is never carried forward: a level ages gracefully, a bolt is a
        # claim about *now*.
        power = _parse_power(power_text or text)
        if percent is None:
            # Keep the last figure; the error says it is no longer being refreshed.
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
        """Force the next space probe (a transfer landed), keeping the figures and their
        `checked_at` on screen meanwhile."""
        self._slot(device_id).space_stale = True

    def refresh_all(self) -> None:
        """Re-read the whole fleet after a devices.yaml edit, or a new `battery:` line sat
        empty for up to `freespace_interval`. Invalidates and lets `_loop` do the reading,
        so an editor's multi-event save cannot start two sweeps."""
        for device in self.devices.config.devices:
            self.invalidate_space(device.id)
        self.rescan_soon(force=True)

    def adopt_space(self, device_id: str, text: str) -> bool:
        """Take a `df` reading the Test button already paid for, so the row it refreshes
        agrees with its dialog. True if it parsed."""
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
        """Device ids whose reachability state just changed, a list per sweep. Read by
        `JobRunner._watch_deferred`, so a deferred job starts when its dot goes green."""
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
                        # Spawned, never awaited: it is bounded at 15 s, and every dot
                        # would wait for it.
                        self.probe_space_soon(device)
                self._loop_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a probe must never kill the loop
                # Logged when the fault changes: from outside, a loop that always raises
                # looks exactly like a dark fleet.
                current = f"{type(exc).__name__}: {exc}"
                if current != self._loop_error:
                    self._loop_error = current
                    log.warning("device probe sweep failed: %s", current)
            await asyncio.sleep(self.settings.probe_interval)

    def _space_stale(self, device_id: str) -> bool:
        """Whether a `df` is worth spawning a task for."""
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
        """Restore last session's readings with their real ages, so each is simply due.

        Of `reach` only `last_ok` returns: it separates amber from red, while `state` must
        be measured now and a restored `next_probe_at` would honour an old backoff. Never
        raises: a corrupt cache costs a cold fleet, not a start-up.
        """
        path = self.settings.probe_cache
        try:
            blob = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(blob, dict) or blob.get("version") != _CACHE_VERSION:
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
        """Write the readings out, at shutdown only, so a restart does not blank a Kobo
        that has been asleep for days. Nothing reads the file while running, so a periodic
        flush would only cost writes. Temp file and rename; never raises."""
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
        # After the cancels, so the readings are final; before the reap, which can run long.
        self.save_cache()
        await reap(list(self._procs))
        self._procs.clear()
        self._task = None
        self._rescan = None
        self._background.clear()


def ssh_base(device: Device, connect_timeout: int = 10) -> list[str]:
    """`ssh` and every option this program passes it, without the destination.

    The one place an ssh command is assembled -- the probe, Test, scans, every `-e`, a
    pull's remote commands -- so none can drift on a keepalive or a timeout. BatchMode
    makes a missing key fail at once instead of waiting on a prompt.

    The keepalives are for the multiplexed master the Pi's ~/.ssh/config opens (a fresh
    handshake to a phone is 680-850 ms, multiplexed 140-355 ms): a phone that sleeps leaves
    the master wedged, and ServerAlive is what notices. It fires only on total silence, so
    no busy transfer trips it; 60 x 3 = 180 s sits under the 300 s space interval, where
    Debian's BatchMode default would be 900 s and 5 x 1 would drop a working link.
    """
    argv = ["ssh", "-p", str(device.effective_port)]
    if device.identity:
        argv += ["-i", str(device.identity)]
    argv += [
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
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
    return argv


def ssh_argv(device: Device, settings: Settings, timeout: int = 10) -> list[str]:
    """`ssh … user@host`, for a caller that appends one remote command."""
    return [*ssh_base(device, timeout), f"{device.effective_user}@{device.host}"]


def rsync_e(device: Device, connect_timeout: int = 10) -> str:
    """The same ssh as the single string rsync's `-e` takes.

    rsync splits it on spaces itself and honours quotes, including the `'"'"'` shlex.join
    writes for a quote inside a word, so an `identity:` path with a space survives. The
    scan's copy of this joined without quoting and did not.
    """
    return shlex.join(ssh_base(device, connect_timeout))


def battery_command(device: Device) -> str | None:
    """The shell fragment that reads this device's charge, or None.

    `battery` is a path and is quoted; `battery_cmd` is a command by declaration -- written
    by someone who already has ssh to the device -- and may be a pipeline, so it is not.
    """
    if device.battery:
        return f"cat {shlex.quote(device.battery)} 2>&1"
    if device.battery_cmd:
        return f"{device.battery_cmd} 2>&1"
    return None


def charging_command(device: Device) -> str | None:
    """The shell fragment that reads whether this device is on a charger, or None.

    A `battery_cmd`'s JSON already says. For a file it is the `status` beside the declared
    `capacity`: sysfs fixes both names within one supply directory, and a missing one draws
    no bolt rather than a wrong one. Never another supply's `online` file -- on lg,
    `charger_controller` reports `online: 1` permanently while the phone is unplugged --
    and never the sign of `CURRENT_NOW`, which lg and bk report positive while
    discharging, the opposite of the Nexus 10. `charging:` overrides the derivation.
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


def df_command(target: str) -> str:
    """`df` of the target in one shell, whichever dialect the device speaks.

    `-Pk` is the portable form on GNU coreutils, but Android's toybox rejects both flags,
    and Termux is a primary target class. Captured first and retried only on empty output:
    toybox prints its table anyway and exits non-zero, so `a || b` printed it twice, and a
    second ssh -- what this used to cost every toybox node every five minutes -- is the
    expensive half of a probe on a sleeping phone.
    """
    t = shlex.quote(target)
    return f'd=`df -Pk {t} 2>/dev/null`; [ -n "$d" ] || d=`df {t} 2>&1`; echo "$d"'


def _readings_script(device: Device) -> str:
    """One shell line that reads everything an ssh round trip can get us at once.

    The battery rides along with `df` rather than opening a second connection: on a
    sleeping Termux node the connection *is* the cost, and two probes on their own
    schedules would also drift out of step in the row that shows both. Marked sections
    rather than positional parsing, because `df` output is one line on some devices and
    two on others -- see `_parse_df`. The Test button runs this same script and more, so
    the two cannot read different things.
    """
    script = f'echo "# df"; {df_command(device.target)}'
    read = battery_command(device)
    if read is not None:
        script += f'; echo "# battery"; {read}'
        charger = charging_command(device)
        if charger is not None:
            script += f'; echo "# power"; {charger}'
    return script


def _section(text: str, name: str) -> str:
    """The `# <name>` block of a marked transcript, up to the next marker; "" if absent."""
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


#: JSON keys meaning "percent charged" (termux-api, upower, Android's intent), matched
#: whole so `percentage_design` cannot answer for the charge.
_BATTERY_KEYS = ("percentage", "capacity", "level", "battery_level")


def _parse_battery(text: str) -> int | None:
    """A reading as a percentage: a bare integer (sysfs) or a JSON object (termux-api),
    else None. Never fished out of a longer message: "No such file or directory (2)" is not
    2% charge."""
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


#: The kernel's `status` words (Android's intent uses the same, upper-snake), mapped to
#: what the row draws. `Full` and `Not charging` both mean attached and not taking.
#: `Unknown` is absent on purpose: the driver not knowing is not a fact about the charger.
_POWER_WORDS = {
    "charging": "charging",
    "full": "plugged",
    "not charging": "plugged",
    "not_charging": "plugged",
    "discharging": "unplugged",
}


def _parse_power(text: str) -> str | None:
    """"charging", "plugged", "unplugged" or None, from a sysfs `status` word or
    termux-api JSON -- where `plugged` says whether a charger is attached and `status`
    whether current flows. Anything unrecognised is None, never a guess."""
    stripped = text.strip()
    if not stripped:
        return None

    if not stripped.startswith("{"):
        # One word, matched whole: a failed `cat`'s path may contain "charging".
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
            return "charging" if flowing == "charging" else "plugged"
        return None

    return flowing


def _battery_error(text: str) -> str:
    """Why a reading did not parse, in a tooltip's few words: a failed `cat`'s last line,
    or the keys a JSON object had instead of a charge."""
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
    """A percentage, rounded, or None when outside 0..100 (then it was not the charge)."""
    if not 0 <= value <= 100:
        return None
    return int(round(value))


def _df_field(token: str) -> int | None:
    """One df size field -> bytes: GNU `-Pk` prints 1K blocks, toybox `466.35G`."""
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
    "ssh_base",
    "rsync_e",
    "parse_size",
]
