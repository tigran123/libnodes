"""The one object every route reaches through: `request.app.state.lib`.

Keeping the wiring here rather than in module globals is what lets the test suite point
a whole app at a fixture library tree without patching imports.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from .config import DevicesStore, Settings
from .probe import DeviceProbe
from .jobs import JobRunner, JobStore
from .library import LibraryIndex
from .manifests import Manifests
from .scan import Scanner
from .watch import FileWatcher


class AppState:
    def __init__(self, settings: Settings, devices: DevicesStore) -> None:
        self.settings = settings
        self.devices = devices
        self.index = LibraryIndex(settings)
        self.manifests = Manifests(settings.manifests_db)
        self.probe = DeviceProbe(settings, devices)
        self.store = JobStore(settings.jobs_db)
        self.jobs = JobRunner(
            settings, self.store, self.index, self.manifests, self.probe, devices
        )
        self.config_watch = FileWatcher(settings.resolved_devices_file)
        self.scanner = Scanner(settings, self.manifests)
        # One worker: a reindex is a disk-bound walk and the Pi has one spindle.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reindex")
        self._reindex_task: asyncio.Task | None = None
        self._reindex_loop_task: asyncio.Task | None = None

    # --- reindex ----------------------------------------------------------

    def reindex_soon(self) -> None:
        """Kick a rebuild if one is not already in flight. Returns immediately."""
        if self.index.running:
            return
        if self._reindex_task is not None and not self._reindex_task.done():
            return
        loop = asyncio.get_running_loop()
        self._reindex_task = loop.create_task(self._reindex())

    async def _reindex(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._pool, self.index.reindex)

    async def _reindex_loop(self) -> None:
        interval = self.settings.reindex_interval
        while True:
            await asyncio.sleep(interval)
            try:
                self.reindex_soon()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self.settings.ensure_dirs()
        self.probe.start()
        self.jobs.start()
        self.config_watch.start()
        if self.settings.reindex_on_start and not self.index.db_path.exists():
            self.reindex_soon()
        if self.settings.reindex_interval > 0:
            self._reindex_loop_task = asyncio.create_task(
                self._reindex_loop(), name="reindex-loop"
            )

    async def stop(self) -> None:
        for task in (self._reindex_loop_task, self._reindex_task):
            if task is not None:
                task.cancel()
        await self.probe.stop()
        await self.jobs.stop()
        await self.config_watch.stop()
        await self.scanner.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)


__all__ = ["AppState"]
