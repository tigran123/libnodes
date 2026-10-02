"""Ask a device what it already holds.

A device populated by other means is invisible until scanned. The listing is `rsync -r
--list-only` over the same ssh -- 35 s for 24,621 entries on an Android phone over Wi-Fi,
so always in the background. It gives size and mtime, never content -- except on a CAS
node, where a link's printed target *is* the blob hash (see `parse_line`).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

from .library import blob_from_link
from .models import Device
from .probe import rsync_e
from .procs import reap

#   -rwxr-x---     21,669,813 2026/08/11 08:32:40 Art/Complete-Book-of-Drawing.pdf
#   drwxr-x---         32,768 2026/08/11 14:22:20 Art
LINE_RE = re.compile(
    r"^([-dlspbc][rwxstST-]{9})\s+([\d,]+)\s+"
    r"(\d{4}/\d{2}/\d{2})\s+(\d{2}:\d{2}:\d{2})\s+(.+)$"
)


@dataclass
class ScanResult:
    files: int = 0
    total_bytes: int = 0
    skipped: int = 0
    error: str | None = None
    duration: float = 0.0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def parse_line(
    line: str, *, keep_links: bool = False
) -> tuple[str, str | None, int, int, bool] | None:
    """One listing line -> ``(path, blob, size, mtime, is_dir)``, or None if unusable.

    Directories are kept (an empty one's row is its only evidence). Symlinks are dropped
    unless `keep_links` -- a CAS node, where they *are* the library -- and then reported as
    files carrying the hash their target names, with size 0 rather than the link's own
    bytes: an exact content claim where a scan is otherwise only a size guess.
    """
    m = LINE_RE.match(line.rstrip("\n"))
    if m is None:
        return None
    perms, size, date, clock, path = m.groups()
    is_dir = perms.startswith("d")
    is_link = perms.startswith("l")
    if not (is_dir or perms.startswith("-") or (is_link and keep_links)):
        return None  # symlink we do not want, socket, device node
    path = path.strip()
    blob = None
    if is_link:
        # `name -> target`, split from the right: a book's name may contain " -> ".
        path, _, target = path.rpartition(" -> ")
        if not path:
            return None
        path = path.strip()
        blob = blob_from_link(target.strip())
    if path.startswith("./"):
        path = path[2:]
    if not path or path == ".":
        return None
    try:
        stamp = datetime.strptime(f"{date} {clock}", "%Y/%m/%d %H:%M:%S").timestamp()
    except ValueError:
        stamp = 0.0
    try:
        return (path, blob, 0 if is_link else int(size.replace(",", "")), int(stamp), is_dir)
    except ValueError:
        return None


def parse_listing(
    lines: Iterator[str], *, keep_links: bool = False
) -> Iterator[tuple[str, str | None, int, int, int]]:
    """Manifest rows from a listing: ``(path, blob, size, mtime, is_dir)``."""
    for line in lines:
        parsed = parse_line(line, keep_links=keep_links)
        if parsed is not None:
            path, blob, size, mtime, is_dir = parsed
            yield (path, blob, 0 if is_dir else size, mtime, int(is_dir))


def demangle(path: str) -> str | None:
    """Recover a filename whose UTF-8 bytes were re-encoded as Latin-1, or None. A real
    device held 17 like ``01 ÐÑÐ·ÑÐºÐ°.flac`` beside correctly named copies."""
    try:
        recovered = path.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    return recovered if recovered != path else None


def scan_argv(device: Device, settings) -> list[str]:
    """``rsync -r --list-only`` over the target. A CAS node also gets `-l`, without which
    rsync lists a symlink but not its `-> …/<hash>` target (verified on rsync 3.4.1)."""
    target = device.target.rstrip("/")
    return [
        "rsync",
        "-r",
        *(["-l"] if device.cas_tree else []),
        "--list-only",
        "-e",
        rsync_e(device),
        f"{device.effective_user}@{device.host}:{target}/",
    ]


class Scanner:
    """Runs device scans one at a time and remembers the outcome per device."""

    def __init__(self, settings, manifests) -> None:
        self.settings = settings
        self.manifests = manifests
        self._results: dict[str, ScanResult] = {}
        self._running: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        #: The live rsync per device, so `stop` can reap it (see procs.reap).
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    def result(self, device_id: str) -> ScanResult | None:
        return self._results.get(device_id)

    def is_running(self, device_id: str) -> bool:
        return device_id in self._running

    def start(self, device: Device) -> bool:
        """Kick a scan. Returns False if one is already in flight for this device."""
        if device.id in self._running:
            return False
        self._running.add(device.id)
        task = asyncio.create_task(self._run(device), name=f"scan-{device.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _run(self, device: Device) -> ScanResult:
        started = time.time()
        result = ScanResult(started_at=started)
        rows: list[tuple[str, str | None, int, int, int]] = []
        # `cas_tree`, not `is_mirror`: an upstream has the shape too.
        keep_links = device.cas_tree
        stderr_task: asyncio.Future | None = None
        try:
            argv = scan_argv(device, self.settings)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._procs[device.id] = proc
            assert proc.stdout is not None and proc.stderr is not None
            # Beside stdout: a full stderr pipe would stall rsync while we wait on stdout.
            stderr_task = asyncio.ensure_future(proc.stderr.read())
            async for raw in proc.stdout:
                parsed = parse_line(
                    raw.decode("utf-8", errors="replace"), keep_links=keep_links
                )
                if parsed is None:
                    result.skipped += 1
                    continue
                path, blob, size, mtime, is_dir = parsed
                if is_dir:
                    rows.append((path, None, 0, mtime, 1))
                else:
                    rows.append((path, blob, size, mtime, 0))
                    result.files += 1
                    result.total_bytes += size

            stderr = await stderr_task
            code = await proc.wait()
            if code != 0:
                tail = stderr.decode(errors="replace").strip().splitlines()
                result.error = tail[-1] if tail else f"rsync exited {code}"
            else:
                self.manifests.replace_scan(device.id, rows, started_at=started)
        except (OSError, asyncio.CancelledError) as exc:
            result.error = str(exc) or exc.__class__.__name__
        except Exception as exc:  # noqa: BLE001 - a scan must never take the app down
            result.error = str(exc)
        finally:
            result.duration = time.time() - started
            result.finished_at = time.time()
            self._results[device.id] = result
            self._running.discard(device.id)
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
            # Only a child that has exited, or `stop()` has nothing to reap.
            proc = self._procs.get(device.id)
            if proc is not None and proc.returncode is not None:
                self._procs.pop(device.id, None)
        return result

    async def stop(self) -> None:
        # Cancel the readers, then reap. See procs.reap.
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await reap(list(self._procs.values()))
        self._procs.clear()
        self._tasks.clear()
        self._running.clear()


__all__ = ["ScanResult", "Scanner", "parse_line", "parse_listing", "scan_argv"]
