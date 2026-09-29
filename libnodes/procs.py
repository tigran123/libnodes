"""Subprocess teardown: terminate, and then actually wait.

asyncio leaves a dead child's transport to the garbage collector, which on shutdown runs
after the loop has closed and raises `RuntimeError: Event loop is closed` from a `__del__`
that names nothing. So after `terminate()`: wait for the child, then close the transport.
Not `communicate()`: a grandchild (rsync's ssh, a script's `sleep`) holds the pipe open,
and an unbounded drain would hang shutdown. Cancel the readers first, *then* reap -- or a
registry that deregisters on cancellation hands reap an empty list.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Iterable

#: SIGTERM's grace before SIGKILL. rsync acts on it in milliseconds and `--partial`
#: resumes a killed transfer, so there is nothing to wait for.
GRACE = 0.5


async def reap(procs: Iterable[asyncio.subprocess.Process | None]) -> None:
    """Stop these subprocesses and leave nothing behind for the garbage collector."""
    # Materialised: this walks it twice, and a generator would be empty the second time.
    procs = [p for p in procs if p is not None]
    alive = [p for p in procs if p.returncode is None]
    for proc in alive:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()

    for proc in alive:
        try:
            await asyncio.wait_for(proc.wait(), timeout=GRACE)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(Exception):  # noqa: BLE001
                await proc.wait()

    for proc in procs:
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):  # noqa: BLE001
                transport.close()


__all__ = ["GRACE", "reap"]
