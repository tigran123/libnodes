"""Subprocess teardown.

The bug these pin produced no failure and no bad output — only two
`RuntimeError: Event loop is closed` tracebacks after every pytest run, from a `__del__`
naming `BaseSubprocessTransport` and nothing that had spawned anything. They were written
down in TODO.md as expected noise for long enough to stop being read.
"""

from __future__ import annotations

import asyncio

from libnodes.procs import GRACE, reap


async def _spawn(script: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def test_reap_waits_for_the_child_to_actually_die():
    """terminate() only asks. Returning before the child has gone is the whole bug."""
    proc = await _spawn("sleep 30")
    assert proc.returncode is None

    await reap([proc])
    assert proc.returncode is not None


async def test_reap_escalates_to_sigkill():
    """A shell parked in `sleep` does not act on SIGTERM until its child returns.

    That is the case GRACE exists for, and the one that must not hang a shutdown.
    """
    proc = await _spawn("trap '' TERM; sleep 30")
    loop = asyncio.get_running_loop()
    started = loop.time()

    await reap([proc])

    assert proc.returncode is not None
    # Killed after the grace, not waited out for thirty seconds.
    assert loop.time() - started < GRACE + 2.0


async def test_reap_releases_the_pipes():
    """A transport finishes only once stdout and stderr reach EOF.

    Nobody is reading them by this point — the reader has just been cancelled — so reap
    closes the transport instead. `_transport` is a private handle because asyncio offers
    no public one; it is also exactly what regressed, so the test names it.
    """
    proc = await _spawn("sleep 30")
    await reap([proc])

    transport = proc._transport
    assert transport is not None
    assert transport.is_closing()


async def test_reap_is_safe_on_a_process_that_already_exited():
    proc = await _spawn("exit 0")
    await proc.wait()

    await reap([proc])          # must not raise ProcessLookupError
    assert proc.returncode == 0


async def test_reap_tolerates_none_and_an_empty_list():
    await reap([])
    await reap([None])


async def test_reap_accepts_a_generator():
    """It walks the collection twice — once to signal, once to release the pipes.

    Handed a generator, a second pass would find it empty and leave every pipe open,
    which is the failure it exists to prevent and would look like success.
    """
    procs = [await _spawn("sleep 30"), await _spawn("sleep 30")]

    await reap(p for p in procs)

    assert all(p.returncode is not None for p in procs)
    assert all(p._transport.is_closing() for p in procs)
