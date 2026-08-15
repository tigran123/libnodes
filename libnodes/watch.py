"""inotify-backed change notification for devices.yaml.

Polling a file's mtime from the browser is the wrong shape twice over: it costs a
request per interval per open page, and it still reports the change late. The kernel
already has the answer — inotify — and `watchfiles` (a uvicorn[standard] dependency, so
nothing new to install) wraps it with an async iterator.

Two deliberate details:

* We watch the **parent directory**, not the file. A file watch is registered against an
  inode, and `$EDITOR` typically saves by writing a temp file and renaming it over the
  target; the watch then survives pointing at an unlinked inode and never fires again.
  vim, emacs and `sed -i` all behave this way.
* inotify drives *latency*, not correctness. `DevicesStore` still stats the file on
  access, so a missed or unavailable event costs freshness on the next request, never a
  stale config. On a filesystem without inotify (NFS, some FUSE mounts) the app simply
  loses the push and keeps working.
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
        self._available = True

    @property
    def available(self) -> bool:
        """False once the watch has failed; callers may fall back to polling."""
        return self._available

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
            # rust_timeout keeps the loop responsive to cancellation; step debounces
            # the burst of events a single save produces.
            async for changes in awatch(
                directory, step=50, rust_timeout=5000, yield_on_timeout=True
            ):
                if any(Path(p).name == target for _, p in changes):
                    self._emit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - inotify is an optimisation, never a hard dep
            self._available = False

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
