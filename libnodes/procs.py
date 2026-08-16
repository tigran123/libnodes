"""Subprocess teardown: terminate, and then actually wait.

Every rsync and ssh this program runs is an asyncio subprocess, and asyncio does not
close the transport when the child dies — it leaves it for the garbage collector. If the
event loop closes first, which on shutdown it always does, that collection calls
`loop.call_soon()` on a dead loop and raises `RuntimeError: Event loop is closed` from
inside a `__del__`. The traceback names `BaseSubprocessTransport.__del__` and nothing
else: not the device, not the command, not the request that started it. The test suite
ended in two of those for long enough that they were written down as expected output.

`terminate()` only asks. Two things have to follow it or the transport stays open:

* **wait for the child.** Returning while it is still dying is the same as not asking.
* **release the pipes.** A transport finishes only once stdout and stderr have reached
  EOF, and the coroutine that was reading them has just been cancelled, so nobody is
  left to read one.

Reading to EOF ourselves — `communicate()` — is the obvious way to do the second, and it
is a trap: a *grandchild* that inherited the pipe holds it open long after its parent is
gone. rsync's own ssh does this, and so does a shell script's `sleep`. Measured on the
test suite, an unbounded drain added five seconds and would have hung a real shutdown for
as long as the orphan lived. Closing the transport releases the same pipes at once and
cannot block. asyncio offers no public handle for it; `_transport` has been the attribute
since 3.8.

Order still matters at every call site: cancel the readers first, *then* reap.

Reaping is also the difference between a clean stop and an rsync still writing to a
device after the service has gone.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Iterable

#: How long SIGTERM gets before SIGKILL follows. rsync acts on it in milliseconds — the
#: Abort button already depends on that — and `--partial` means a killed transfer resumes
#: byte-accurate, so there is nothing to be gained by waiting longer. Half a second rather
#: than a couple, because a shell parked in `sleep` does not act on SIGTERM until its
#: child returns: at 2.0 the two test fixtures that do exactly that cost four seconds of
#: the suite, and at 0.5 they cost one.
GRACE = 0.5


async def reap(procs: Iterable[asyncio.subprocess.Process | None]) -> None:
    """Stop these subprocesses and leave nothing behind for the garbage collector."""
    # Materialised once: this walks the collection twice, and a caller handing over a
    # generator would otherwise find the second pass empty and the pipes still open.
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

    # Release stdout and stderr. Without this the transport waits on an EOF that a
    # cancelled reader will never consume, and gets collected after the loop has closed.
    for proc in procs:
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):  # noqa: BLE001
                transport.close()


__all__ = ["GRACE", "reap"]
