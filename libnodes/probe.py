"""Reachability and free-space probes for the configured devices.

Requests never probe. A background task TCP-connects to every node on a fixed interval
and writes into a dict; handlers read the dict. That is what keeps `/devices/rows` — which
the browser polls every 10s — from turning six sleeping e-readers into a six-second
page load.

Free space is a second, much slower probe (it costs a real ssh round trip), so it runs
on its own longer interval and is refreshed opportunistically after a transfer.
"""

from __future__ import annotations

import asyncio
import errno
import shlex
import time
from dataclasses import dataclass, field, replace
from typing import Literal

from .config import DevicesStore, Settings
from .models import Device, parse_size
from .procs import reap

State = Literal["online", "sleeping", "offline", "unknown"]


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


@dataclass
class _Slot:
    reach: Reachability = field(default_factory=Reachability)
    space: FreeSpace = field(default_factory=FreeSpace)
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

    # --- accessors --------------------------------------------------------

    def _slot(self, device_id: str) -> _Slot:
        return self._slots.setdefault(device_id, _Slot())

    def status(self, device_id: str) -> Reachability:
        return self._slot(device_id).reach

    def space(self, device_id: str) -> FreeSpace:
        return self._slot(device_id).space

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

    def _backoff(self, failures: int) -> float:
        """Exponential, capped. A node dead for an hour is not news every 10 seconds."""
        delay = self.settings.probe_interval * (2 ** min(failures - 1, 8))
        return min(delay, self.settings.probe_backoff_max)

    def due(self, device: Device, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return self._slot(device.id).reach.next_probe_at <= now

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
            for command in (f"df -Pk {target}", f"df {target}"):
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
                    self._procs.discard(proc)

                stderr_tail = err.decode(errors="replace").strip().splitlines()
                if stderr_tail:
                    last_error = stderr_tail[-1]
                parsed = _parse_df(out.decode(errors="replace"))
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

    def invalidate_space(self, device_id: str) -> None:
        """Force the next space probe, e.g. right after a transfer landed."""
        slot = self._slot(device_id)
        slot.space = replace(slot.space, checked_at=None)

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
                    if self.status(device.id).online:
                        await self.probe_space(device)
                    # Offline nodes are skipped entirely: probe_space already returns
                    # early for them, and there is nothing to ask a dead host.
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a probe must never kill the loop
                pass
            await asyncio.sleep(self.settings.probe_interval)

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
    "DeviceProbe",
    "FreeSpace",
    "Reachability",
    "State",
    "ssh_argv",
    "parse_size",
]
