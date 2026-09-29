"""The one object every route reaches through: `request.app.state.lib`.

Keeping the wiring here rather than in module globals is what lets the test suite point
a whole app at a fixture library tree without patching imports.
"""

from __future__ import annotations

import asyncio
import contextlib
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
            settings,
            self.store,
            self.index,
            self.manifests,
            self.probe,
            devices,
            # A pull is the one job that changes library_root.
            on_library_changed=self.reindex,
        )
        self.config_watch = FileWatcher(settings.resolved_devices_file)
        self.scanner = Scanner(settings, self.manifests)
        # One worker: a reindex is one disk-bound walk at a time.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reindex")
        self._reindex_task: asyncio.Task | None = None
        #: A rebuild was asked for while one was running, and that one may have walked the
        #: tree before the change that prompted the ask -- a pull finishing mid-walk.
        self._reindex_again = False
        self._reindex_loop_task: asyncio.Task | None = None
        self._config_reload_task: asyncio.Task | None = None

    # --- reindex ----------------------------------------------------------

    def reindex_soon(self) -> None:
        """Kick a rebuild, or ask the running one to go again. Returns immediately.

        Dropping the ask while a walk was in flight, as this once did, left a pull's books
        out of the index until the next 30-minute tick whenever the pull finished
        mid-walk: the walk had already passed the directories the pull wrote into.
        """
        if self._reindex_task is not None and not self._reindex_task.done():
            self._reindex_again = True
            return
        loop = asyncio.get_running_loop()
        self._reindex_task = loop.create_task(self._reindex())

    async def reindex(self) -> None:
        """Rebuild the index -- or join the rebuild already running -- and wait for it.

        For a pull, which ends by saying the index now holds what it brought. Shielded, so
        a job cancelled on shutdown does not take a half-done walk down with it.
        """
        self.reindex_soon()
        if self._reindex_task is not None:
            await asyncio.shield(self._reindex_task)

    async def _reindex(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            self._reindex_again = False
            await loop.run_in_executor(self._pool, self.index.reindex)
            if not self._reindex_again:
                return

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

    # --- devices.yaml -----------------------------------------------------

    async def _config_reload_loop(self) -> None:
        """Re-read the whole fleet's readings whenever devices.yaml changes: the config
        reloads on its own, but a new `battery:` line or a corrected host would otherwise
        wait out `freespace_interval`. See `DeviceProbe.refresh_all`."""
        queue = self.config_watch.subscribe()
        try:
            while True:
                await queue.get()
                # One save is several events.
                await asyncio.sleep(0.05)
                while not queue.empty():
                    queue.get_nowait()
                try:
                    self.probe.refresh_all()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a bad edit must not kill the watcher
                    pass
        finally:
            self.config_watch.unsubscribe(queue)

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self.settings.ensure_dirs()
        self.probe.start()
        self.jobs.start()
        self.config_watch.start()
        self._config_reload_task = asyncio.create_task(
            self._config_reload_loop(), name="config-reload"
        )
        if self.settings.reindex_on_start and not self.index.db_path.exists():
            self.reindex_soon()
        if self.settings.reindex_interval > 0:
            self._reindex_loop_task = asyncio.create_task(
                self._reindex_loop(), name="reindex-loop"
            )

    async def stop(self) -> None:
        for task in (
            self._reindex_loop_task,
            self._reindex_task,
            self._config_reload_task,
        ):
            if task is not None:
                task.cancel()
        # Awaited: it holds a subscription config_watch.stop() clears.
        if self._config_reload_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._config_reload_task
            self._config_reload_task = None
        await self.probe.stop()
        await self.jobs.stop()
        await self.config_watch.stop()
        await self.scanner.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)


__all__ = ["AppState"]
