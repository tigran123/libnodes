"""Ask a device what it already holds.

A device that was populated by some other means — a card reader, an older script, a
previous life — is invisible to LibNodes until it is scanned. Without this the Library
view shows `—` for every row on a device holding the entire library, and there is no way
to tell "not sent yet" from "sent long ago by other means".

The listing comes from ``rsync -r --list-only``, which needs nothing the push path does
not already need: the same ssh, the same remote rsync. Measured against a real Android
device over WiFi: **35 s for 24,621 entries**, so it runs in the background and never in
a request.

What a listing cannot give us is content. It reports size and mtime only, so a scanned
manifest row is a weaker claim than a pushed one — see `manifests._compare`.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

from .models import Device
from .probe import ssh_argv

#   -rwxr-x---     21,669,813 2026/08/11 08:32:40 Art/Complete-Book-of-Drawing.pdf
#   drwxr-x---         32,768 2026/08/11 14:22:20 Art
LINE_RE = re.compile(
    r"^([-dlspbc][rwxstST-]{9})\s+([\d,]+)\s+"
    r"(\d{4}/\d{2}/\d{2})\s+(\d{2}:\d{2}:\d{2})\s+(.+)$"
)


@dataclass
class ScanResult:
    files: int = 0
    dirs: int = 0
    total_bytes: int = 0
    skipped: int = 0
    error: str | None = None
    duration: float = 0.0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def parse_line(line: str) -> tuple[str, int, int, bool] | None:
    """One listing line -> ``(path, size, mtime, is_dir)``, or None if unusable.

    Directories are kept, not discarded: an empty directory contains no files to count,
    so its own row is the only evidence that it exists on the device at all. Symlinks,
    device nodes and rsync's `.` self-entry are dropped.
    """
    m = LINE_RE.match(line.rstrip("\n"))
    if m is None:
        return None
    perms, size, date, clock, path = m.groups()
    is_dir = perms.startswith("d")
    if not (is_dir or perms.startswith("-")):
        return None  # symlink, socket, device node
    path = path.strip()
    if path.startswith("./"):
        path = path[2:]
    if not path or path == ".":
        return None
    try:
        stamp = datetime.strptime(f"{date} {clock}", "%Y/%m/%d %H:%M:%S").timestamp()
    except ValueError:
        stamp = 0.0
    try:
        return (path, int(size.replace(",", "")), int(stamp), is_dir)
    except ValueError:
        return None


def parse_listing(lines: Iterator[str]) -> Iterator[tuple[str, None, int, int, int]]:
    """Manifest rows from a listing: ``(path, blob, size, mtime, is_dir)``.

    `blob` is always None — a remote listing cannot tell us content.
    """
    for line in lines:
        parsed = parse_line(line)
        if parsed is not None:
            path, size, mtime, is_dir = parsed
            yield (path, None, 0 if is_dir else size, mtime, int(is_dir))


def demangle(path: str) -> str | None:
    """Recover a filename whose UTF-8 bytes were re-encoded as Latin-1, or None.

    A real device turned up 17 files named like ``01 ÐÑÐ·ÑÐºÐ°.flac`` — the UTF-8 bytes
    of ``01 Музыка.flac`` each expanded into their own two-byte sequence, by whatever
    wrote them originally. rsync cannot see that as the same file, so it copies a second
    correct-named copy alongside and the device quietly carries both.

    Reversing the mistake turns a wall of gibberish into "this is a duplicate of X",
    which is the difference between a listing you can act on and one you cannot.
    """
    try:
        recovered = path.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    return recovered if recovered != path else None


def scan_argv(device: Device, settings) -> list[str]:
    """``rsync -r --list-only`` over the device's target directory."""
    ssh = ssh_argv(device, settings)
    remote = f"{device.effective_user}@{device.host}"
    # ssh_argv ends with user@host; everything before it is the ssh command itself.
    ssh_cmd = " ".join(a for a in ssh[:-1])
    target = device.target.rstrip("/")
    return [
        "rsync",
        "-r",
        "--list-only",
        "-e",
        ssh_cmd,
        f"{remote}:{target}/",
    ]


class Scanner:
    """Runs device scans one at a time and remembers the outcome per device."""

    def __init__(self, settings, manifests) -> None:
        self.settings = settings
        self.manifests = manifests
        self._results: dict[str, ScanResult] = {}
        self._running: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

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
        rows: list[tuple[str, None, int, int, int]] = []
        try:
            argv = scan_argv(device, self.settings)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert proc.stdout is not None
            async for raw in proc.stdout:
                parsed = parse_line(raw.decode("utf-8", errors="replace"))
                if parsed is None:
                    result.skipped += 1
                    continue
                path, size, mtime, is_dir = parsed
                if is_dir:
                    rows.append((path, None, 0, mtime, 1))
                    result.dirs += 1
                else:
                    rows.append((path, None, size, mtime, 0))
                    result.files += 1
                    result.total_bytes += size

            stderr = await proc.stderr.read()
            code = await proc.wait()
            if code != 0:
                tail = stderr.decode(errors="replace").strip().splitlines()
                result.error = tail[-1] if tail else f"rsync exited {code}"
            else:
                # Replace wholesale: a scan is authoritative about what is there now,
                # so a file deleted on the device must disappear from the manifest too.
                self.manifests.replace_scan(device.id, rows)
        except (OSError, asyncio.CancelledError) as exc:
            result.error = str(exc) or exc.__class__.__name__
        except Exception as exc:  # noqa: BLE001 - a scan must never take the app down
            result.error = str(exc)
        finally:
            result.duration = time.time() - started
            result.finished_at = time.time()
            self._results[device.id] = result
            self._running.discard(device.id)
        return result

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        self._running.clear()


__all__ = ["ScanResult", "Scanner", "parse_line", "parse_listing", "scan_argv"]
