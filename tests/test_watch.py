"""inotify-driven config reload.

Polling a file's mtime from the browser is the wrong shape: a request per interval per
open page, and the change still arrives late. These tests pin the kernel-notification
path, including the case that breaks a naive implementation — `$EDITOR` saving by
rename.
"""

from __future__ import annotations

import asyncio

import pytest

from libnodes.watch import FileWatcher


async def _wait(queue: asyncio.Queue, timeout: float = 5.0) -> bool:
    try:
        await asyncio.wait_for(queue.get(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def test_write_is_noticed(tmp_path):
    target = tmp_path / "devices.yaml"
    target.write_text("devices: []\n")

    watcher = FileWatcher(target)
    watcher.start()
    await asyncio.sleep(0.3)  # let the watch establish
    queue = watcher.subscribe()

    target.write_text("devices: [x]\n")
    assert await _wait(queue), "inotify did not report the write"

    await watcher.stop()


async def test_save_by_rename_is_noticed(tmp_path):
    """The case a file-inode watch misses entirely.

    vim, emacs and `sed -i` all write a temp file and rename it over the target. A watch
    registered on the original inode survives pointing at an unlinked file and never
    fires again — which is why this watches the parent directory.
    """
    target = tmp_path / "devices.yaml"
    target.write_text("devices: []\n")

    watcher = FileWatcher(target)
    watcher.start()
    await asyncio.sleep(0.3)
    queue = watcher.subscribe()

    tmp = tmp_path / "devices.yaml.tmp"
    tmp.write_text("devices: [renamed]\n")
    tmp.replace(target)

    assert await _wait(queue), "rename-over-target went unnoticed"

    await watcher.stop()


async def test_unrelated_files_are_ignored(tmp_path):
    """The directory holds index.db, jobs.db and logs; only devices.yaml matters."""
    target = tmp_path / "devices.yaml"
    target.write_text("devices: []\n")

    watcher = FileWatcher(target)
    watcher.start()
    await asyncio.sleep(0.3)
    queue = watcher.subscribe()

    (tmp_path / "jobs.db").write_text("noise")
    (tmp_path / "index.db").write_text("more noise")
    await asyncio.sleep(0.6)

    assert queue.empty(), "a sibling file woke the config watcher"

    await watcher.stop()


async def test_subscribers_are_independent(tmp_path):
    target = tmp_path / "devices.yaml"
    target.write_text("devices: []\n")

    watcher = FileWatcher(target)
    watcher.start()
    await asyncio.sleep(0.3)
    a, b = watcher.subscribe(), watcher.subscribe()

    target.write_text("devices: [x]\n")
    assert await _wait(a)
    assert await _wait(b)

    watcher.unsubscribe(a)
    await watcher.stop()


async def test_stop_is_idempotent_and_clears_subscribers(tmp_path):
    target = tmp_path / "devices.yaml"
    target.write_text("devices: []\n")
    watcher = FileWatcher(target)
    watcher.start()
    await asyncio.sleep(0.2)
    watcher.subscribe()
    await watcher.stop()
    await watcher.stop()
    assert watcher._subs == set()


async def test_correctness_does_not_depend_on_the_watcher(client, devices_file):
    """inotify is for latency only.

    The store stats the file on access, so even with no watcher running at all a change
    is picked up on the next request. A filesystem without inotify loses the push, not
    the data.
    """
    assert "Renamed Kobo" not in (await client.get("/devices/rows")).text
    devices_file.write_text(
        devices_file.read_text().replace("name: Test Kobo", "name: Renamed Kobo")
    )
    assert "Renamed Kobo" in (await client.get("/devices/rows")).text


async def test_nothing_polls_for_the_config_any_more(client):
    """The watcher is the mechanism, and the page it used to push to has gone.

    What is left has to stay push-free: the Devices page polls its *rows* every 10s and
    that is a reachability cadence, not a config one. A config poll reappearing here would
    mean somebody had quietly stopped trusting inotify.
    """
    page = await client.get("/devices")
    assert "sse-connect" not in page.text
    assert "every 5s" not in page.text
