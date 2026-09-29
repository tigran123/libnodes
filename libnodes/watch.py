"""inotify change notification for devices.yaml, via `watchfiles` (a uvicorn[standard]
dependency).

It watches the **parent directory**: editors save by rename, and a watch on the file would
survive pointing at an unlinked inode. inotify only drives latency -- `DevicesStore` still
checks the mtime on access, so a missed event costs nothing but a few seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path


class FileWatcher:
    """Fans out 'this file changed' to any number of async subscribers."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._subs: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _emit(self) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(True)
            except asyncio.QueueFull:
                # A subscriber already has an unread change pending; one is enough.
                pass

    async def _run(self) -> None:
        from watchfiles import awatch

        target = self.path.name
        directory = self.path.parent
        try:
            # rust_timeout keeps it cancellable; step debounces one save's burst.
            async for changes in awatch(
                directory, step=50, rust_timeout=5000, yield_on_timeout=True
            ):
                if any(Path(p).name == target for _, p in changes):
                    self._emit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - inotify is an optimisation, never a hard dep
            pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="devices-yaml-watch")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self._subs.clear()


__all__ = ["FileWatcher"]
