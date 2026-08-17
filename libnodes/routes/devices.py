"""Devices view: every device in devices.yaml and whether it answers right now."""

from __future__ import annotations

import asyncio
import shlex
import time
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..deps import base_context, state
from ..probe import (
    Battery,
    FreeSpace,
    Reachability,
    _parse_battery,
    _section,
    battery_command,
    ssh_argv,
)
from ..config import SKIP_TOPLEVEL
from ..procs import reap
from ..scan import scan_argv
from ..jobs import (
    Job,
    build_argv,
    full_sync_sources,
    hints_for_text,
    mirror_sources,
)
from ..manifests import Extras
from ..models import Device
from ..state import AppState
from ..templating import reltime, templates, until

router = APIRouter()


#: What a failed connect implies, keyed on the string `_describe` produced. `sleeping`
#: says only "the connect failed and the node answered within sleeping_window" — it
#: carries no diagnosis of its own, so any reading of one has to come from the error.
#: Mirrors the `connection refused` entry in jobs.py's `_HINTS`, which is what the
#: connection-test dialog shows for the same failure.
_REACH_NOTES: list[tuple[str, str]] = [
    (
        "connection refused",
        "the host answered but nothing is listening on that port — Termux's sshd stops "
        "when the device sleeps",
    ),
    (
        "timed out",
        "nothing answered at all — the device is off, off this network, or asleep below "
        "the network layer",
    ),
    (
        "no route to host",
        "the host is not on this network; a DHCP lease may have moved it",
    ),
    ("network unreachable", "this host has no route to that network"),
]


@dataclass
class DeviceView:
    """One device row: config, live reachability, storage, and any running transfer."""

    device: Device
    reach: Reachability
    space: FreeSpace
    battery: Battery
    last_sync: float | None
    job: Job | None
    #: The connection test as a shell line, for the row button's tooltip. The test is the
    #: one action safe enough to run straight from the row, so it does not get the
    #: dialog's command strip — but "every action shows the command it will run" still
    #: holds, and the result echoes the same line. Built by `_test_argv`, once, so the
    #: two cannot disagree.
    test_command: str = ""

    @property
    def state(self) -> str:
        if self.job is not None and self.job.state == "running":
            return "syncing"
        return self.reach.state

    @property
    def dot_class(self) -> str:
        if self.state == "syncing":
            return "dot-accent dot-pulse"
        return self.reach.dot_class

    @property
    def row_class(self) -> str:
        return {
            "syncing": "is-active",
            "offline": "is-offline",
        }.get(self.state, "")

    @property
    def offline(self) -> bool:
        return self.state == "offline"

    @property
    def sleeping(self) -> bool:
        return self.state == "sleeping"

    @property
    def online(self) -> bool:
        """Green. Every device action needs this — offering them otherwise just
        produces a failure the user could have been spared."""
        return self.state == "online"

    @property
    def reach_note(self) -> str:
        """The tooltip behind a failed row: a reading of the error, when it last
        answered, and how old the reading itself is. A reading, not the fact — the fact is
        the error itself, which the row prints. Here rather than in the template because it
        interprets, and templates only format.

        The age is not decoration. The row is re-rendered every 10s but the probe behind it
        backs off to five minutes, so without it a dot silently asserts a measurement it
        did not just take — a device that came back four minutes ago looks identical to one
        that is still down, and the page gives you no way to tell. Chasing exactly that
        cost an afternoon.
        """
        if self.online or not self.reach.error:
            return ""
        error = self.reach.error.lower()
        note = next((n for needle, n in _REACH_NOTES if needle in error), "")
        seen = (
            f"last answered {reltime(self.reach.last_ok)}"
            if self.reach.last_ok
            else "has never answered"
        )
        checked = (
            f"checked {reltime(self.reach.checked_at)}, "
            f"next {until(self.reach.next_probe_at)}"
            if self.reach.checked_at
            else "not checked yet"
        )
        parts = [p for p in (note, seen, checked) if p]
        return " · ".join(parts)

    @property
    def capacity(self) -> int | None:
        """Prefer what the device reported; fall back to the declared figure."""
        return self.space.total or self.device.capacity_bytes

    @property
    def free(self) -> int | None:
        return self.space.free

    @property
    def used(self) -> int | None:
        """What the Storage column prints, because it is what the bar draws.

        `_parse_df` sets total/used/free together or not at all, and the declared-capacity
        fallback (probe.py) leaves both used and free None, so this is None in exactly the
        cases `free` was.
        """
        return self.space.used

    @property
    def used_pct(self) -> float:
        total = self.capacity
        if not total or self.space.used is None:
            return 0.0
        return max(0.0, min(100.0, 100.0 * self.space.used / total))

    @property
    def has_battery(self) -> bool:
        """Whether this device reports a battery at all.

        Keyed on the declaration, not on the reading: a node with a battery source set
        that has not answered yet must render an empty cell rather than no cell, or the
        column would appear and disappear under the poll.
        """
        return bool(self.device.battery or self.device.battery_cmd)

    @property
    def battery_source(self) -> str:
        """The file or command the reading came from, for the tooltip — so a cell that is
        empty or wrong names the thing to go and check."""
        return self.device.battery or self.device.battery_cmd or ""

    @property
    def battery_pct(self) -> float:
        return float(self.battery.percent or 0)

    @property
    def battery_class(self) -> str:
        """The bar's tint — a colour modifier only, composed onto `track track-2` by the
        template, since `track-2` is a height and these are not.

        Storage fills up as it gets worse and a battery empties, so the two cannot share
        a threshold: this is low-is-bad, at the levels a phone itself warns at.
        """
        pct = self.battery.percent
        if pct is None:
            return ""
        if pct <= 15:
            return "track-err"
        if pct <= 30:
            return "track-warn"
        return ""

    @property
    def battery_note(self) -> str:
        """The cell's tooltip: the reading, its age, and why it is missing if it is."""
        if self.battery.error:
            stale = (
                f"last read {reltime(self.battery.checked_at)}"
                if self.battery.checked_at
                else "never read"
            )
            return f"{self.battery_source}: {self.battery.error} · {stale}"
        if self.battery.percent is None:
            return f"{self.battery_source} — not read yet"
        return f"{self.battery.percent}% · read {reltime(self.battery.checked_at)}"


def device_views(app: AppState) -> list[DeviceView]:
    running = {j.device_id: j for j in app.jobs.active() if j.state == "running"}
    out = []
    for device in app.devices.config.devices:
        out.append(
            DeviceView(
                device=device,
                reach=app.probe.status(device.id),
                space=app.probe.space(device.id),
                battery=app.probe.battery(device.id),
                last_sync=app.manifests.last_sync(device.id),
                job=running.get(device.id),
                test_command=_shell(_test_argv(device, app.settings)),
            )
        )
    return out


def _filtered(views: list[DeviceView], q: str | None) -> list[DeviceView]:
    if not q:
        return views
    needle = q.lower().strip()
    return [
        v
        for v in views
        if needle in v.device.name.lower()
        or needle in v.device.id.lower()
        or needle in v.device.host.lower()
        or needle in v.device.type.lower()
        or needle in v.device.target.lower()
    ]


def devices_context(request: Request, q: str | None = None) -> dict:
    app = state(request)
    # A stamp, not a probe -- this stays inside "requests never probe a device". It tells
    # the background loop somebody is looking, which tightens the backoff ceiling from five
    # minutes to thirty seconds for as long as the page keeps polling. Every devices route
    # funnels through here, so the row poll alone is enough to hold it.
    app.probe.note_interest()
    views = _filtered(device_views(app), q)
    online, total = app.probe.reachable_count
    ctx = base_context(request, "devices")
    ctx.update(
        {
            "nodes": views,
            "q": q or "",
            "online": online,
            "total": total,
            "last_scan": app.probe.last_scan,
            "profiles": app.devices.config.profiles,
        }
    )
    return ctx


@router.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request, q: str | None = None, view: str = "table"):
    ctx = devices_context(request, q)
    ctx["view"] = view if view in ("table", "grid") else "table"
    return templates.TemplateResponse(request, "devices.html", ctx)


@router.get("/devices/rows", response_class=HTMLResponse)
async def device_rows(request: Request, q: str | None = None):
    return templates.TemplateResponse(request, "device_rows.html", devices_context(request, q))


@router.get("/devices/grid", response_class=HTMLResponse)
async def device_grid(request: Request, q: str | None = None):
    return templates.TemplateResponse(request, "device_grid.html", devices_context(request, q))


@router.get("/devices/status", response_class=HTMLResponse)
async def device_status(request: Request):
    """The top-bar chips — polled alongside the table."""
    return templates.TemplateResponse(request, "device_status.html", devices_context(request))


def _one(request: Request, device_id: str) -> DeviceView | None:
    for view in device_views(state(request)):
        if view.device.id == device_id:
            return view
    return None


@router.get("/device/{device_id}/row", response_class=HTMLResponse)
async def device_row(request: Request, device_id: str):
    view = _one(request, device_id)
    if view is None:
        return HTMLResponse("", status_code=404)
    ctx = base_context(request, "devices")
    ctx["node"] = view
    return templates.TemplateResponse(request, "device_row.html", ctx)


@router.post("/device/{device_id}/probe", response_class=HTMLResponse)
async def device_probe(request: Request, device_id: str):
    """Re-probe one device.

    The TCP connect is awaited because the user asked for it and it is bounded by
    `probe_timeout`. The `df` probe is not — it spawns ssh and can take 15s, which has
    no business sitting in a request.
    """
    app = state(request)
    device = app.devices.device(device_id)
    if device is None:
        return HTMLResponse("", status_code=404)
    await app.probe.probe(device)
    if app.probe.status(device_id).online:
        app.probe.probe_space_soon(device, force=True)
    return await device_row(request, device_id)


@router.post("/devices/rescan", response_class=HTMLResponse)
async def devices_rescan(request: Request, q: str | None = None):
    """Sweep every device, ignoring backoff — without making the browser wait.

    Awaiting this would make Rescan cost one connect timeout per unreachable node. The
    returned fragment schedules a single follow-up refresh to pick up the results.
    """
    app = state(request)
    app.probe.rescan_soon(force=True)
    ctx = devices_context(request, q)
    ctx["rescanning"] = True
    return templates.TemplateResponse(request, "device_rows.html", ctx)


def _shell(argv: list[str]) -> str:
    """An argv rendered as the shell line it is equivalent to — for display only."""
    return " ".join(shlex.quote(a) for a in argv)


def _preview(build) -> str:
    """A command strip, or the reason there is no command to show.

    `build_argv` refuses to compose a mirror push it cannot make safe — an empty source
    list, a target at the root — because `--delete` turns either into data loss. The menu
    is rendered from those same calls, so it has to survive the refusal: show why, rather
    than 500 on a dialog whose whole job is to say what will run.
    """
    try:
        return _shell(build())
    except ValueError as exc:
        return f"unavailable — {exc}"


def _whole_root_sources(app: AppState, device: Device) -> list[str]:
    """Everything this device's mode considers "the whole library".

    Two different answers, and every whole-root action wants the one matching the device:
    a reader gets the browsable categories, a mirror gets the entire root including the
    vault it needs for its symlinks to resolve.
    """
    if device.is_mirror:
        return mirror_sources(app.settings)
    return full_sync_sources(app.settings)


@router.get("/device/{device_id}/menu", response_class=HTMLResponse)
async def device_menu(request: Request, device_id: str):
    """Every action for one device, each showing the command it will actually run.

    An action whose effect you have to infer from its label is a bad action — "Adopt
    existing copy" means nothing until you see the `--size-only` that makes it safe.
    """
    app = state(request)
    device = app.devices.device(device_id)
    if device is None:
        return HTMLResponse("", status_code=404)

    config = app.devices.config
    # A mirror's every action is over the whole root including the vault, so the sources
    # differ per mode rather than per action. build_argv reads the mode off the device.
    sources = _whole_root_sources(app, device)
    files, total_bytes, _last = app.manifests.summary(device_id)

    ctx = base_context(request, "devices")
    ctx.update(
        {
            "device": device,
            "node": _one(request, device_id),
            "manifest_files": files,
            "manifest_bytes": total_bytes,
            "scan": app.scanner.result(device_id),
            "scanning": app.scanner.is_running(device_id),
            "commands": {
                # `full_sync` and `replicate` are the same argv; they are two keys because
                # they are two different promises, and the dialog prints the promise beside
                # the command. Full Sync never deletes; Replicate is defined by --delete.
                "full_sync": _preview(
                    lambda: build_argv(device, config, sources, app.settings)
                ),
                "replicate": _preview(
                    lambda: build_argv(device, config, sources, app.settings)
                ),
                "dry_run": _preview(
                    lambda: build_argv(
                        device, config, sources, app.settings, dry_run=True
                    )
                ),
                "adopt": _preview(
                    lambda: build_argv(device, config, sources, app.settings, adopt=True)
                ),
                "scan": _shell(scan_argv(device, app.settings)),
            },
            "library_root": str(app.settings.library_root),
        }
    )
    return templates.TemplateResponse(request, "dialogs/device_menu.html", ctx)


#: One remote probe answering the three questions the design's test strip asks: is it
#: reachable, does it have rsync, is the target writable. Deliberately read-only —
#: `test -w` rather than creating a probe file on someone's device.
#: `df -Pk` is the portable form on GNU coreutils, but Android's toybox rejects the
#: flags, prints its output anyway and exits non-zero — so a plain `a || b` runs df
#: twice and prints the table twice. Capture first, fall back only on empty output.
_TEST_DF = (
    'echo "# df"; d=`df -Pk {t} 2>/dev/null`; '
    '[ -n "$d" ] || d=`df {t} 2>&1`; echo "$d"; '
)
_TEST_TAIL = (
    'echo "# rsync"; rsync --version 2>/dev/null | head -1 || echo "rsync: not found"; '
    'echo "# write"; if test -w {t}; then echo "writable"; else echo "NOT writable"; fi'
)


def _test_script(device: Device) -> str:
    """The connection test, with a battery section for a device that declares one.

    Built per device rather than as one constant, because the battery source is per
    device — and read through `battery_command` so the quoting rule (a path is quoted, a
    command line is not) lives in exactly one place and cannot drift from the background
    probe's copy of the same decision.
    """
    target = shlex.quote(device.target)
    read = battery_command(device)
    # Each half formatted before the battery fragment is joined on, never after: a
    # battery_cmd is free-form shell and may well contain braces -- `awk '{print $1}'` --
    # which str.format would then try to read as a field name and raise on.
    battery = f'echo "# battery"; {read}; ' if read else ""
    return _TEST_DF.format(t=target) + battery + _TEST_TAIL.format(t=target)


def _test_argv(device: Device, settings) -> list[str]:
    """The connection test as one argv, built in exactly one place.

    The row's tooltip and the line echoed above the output both come from here, so they
    cannot drift from what runs. They used to: the Actions dialog advertised a bare
    `df -Pk <target>` while the handler ran this three-part script.
    """
    return [
        *ssh_argv(device, settings),
        _test_script(device),
    ]


@router.post("/device/{device_id}/test", response_class=HTMLResponse)
async def device_test(request: Request, device_id: str):
    """ssh in and report back in a dialog of its own.

    Driven from the device row, not from behind Actions: it reads `df`, the rsync version
    and `test -w`, writes nothing, and is therefore the one action that does not need its
    command read before it is pressed. The result carries the echoed command, the output,
    and a one-line verdict — with the failure case naming a likely cause, which is
    exactly what you want from a device that is not answering.
    """
    app = state(request)
    device = app.devices.device(device_id)
    if device is None:
        return HTMLResponse("", status_code=404)

    argv = _test_argv(device, app.settings)
    started = time.perf_counter()
    out = err = ""
    code: int | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
            out = stdout.decode(errors="replace")
            err = stderr.decode(errors="replace")
            code = proc.returncode
        except asyncio.TimeoutError:
            # kill() only asks; reap waits, so the ssh is gone before we answer.
            await reap([proc])
            err = "timed out after 20s"
    except OSError as exc:
        err = str(exc)

    elapsed = time.perf_counter() - started
    # Re-probe reachability too, so the row behind the dialog agrees with the strip.
    await app.probe.probe(device)
    # And keep the `df` this test just ran. `probe()` above refreshes reachability only,
    # so without this the Storage cell kept whatever the last space probe left there —
    # up to freespace_interval (5 min) old, and visibly disagreeing with the figure in
    # the dialog printed from the same command. The reading is free: we have already
    # paid for the ssh.
    app.probe.adopt_space(device_id, _section(out, "df"))
    if battery_command(device):
        app.probe.adopt_battery(device_id, _section(out, "battery"))

    ctx = base_context(request, "devices")
    ctx.update(
        {
            "device": device,
            "command": _shell(argv),
            "stdout": out.strip(),
            "stderr": err.strip(),
            "code": code,
            "elapsed": elapsed,
            "summary": _test_summary(out) if code == 0 else None,
            "hints": hints_for_text(f"{out}\n{err}", code if code is not None else 255),
            # Built after both updates above, so the row the dialog carries out of band
            # shows the reachability and the storage this test just measured rather than
            # the last poll's.
            "node": _one(request, device_id),
            "oob": True,
        }
    )
    return templates.TemplateResponse(request, "dialogs/test_result.html", ctx)


def _test_summary(out: str) -> list[str]:
    """Turn the probe's output into the design's one-line verdict."""
    bits = []
    if "# df" in out:
        bits.append("reachable")
    # The charge, when the test read one. Parsed rather than echoed, so what the verdict
    # claims is the same figure the row's bar draws — the raw JSON is right there in the
    # transcript below for anyone who wants it.
    charge = _parse_battery(_section(out, "battery"))
    if charge is not None:
        bits.append(f"battery {charge}%")
    for line in out.splitlines():
        if line.startswith("rsync  version") or line.startswith("rsync version"):
            bits.append(line.strip().split(" protocol")[0].strip())
        elif line.strip() == "writable":
            bits.append("target writable")
        elif line.strip() == "NOT writable":
            bits.append("target NOT writable")
        elif line.startswith("rsync: not found"):
            bits.append("no rsync on device")
    return bits


@router.post("/device/{device_id}/scan", response_class=HTMLResponse)
async def device_scan(request: Request, device_id: str):
    """Ask the device what it already holds.

    Runs in the background — a real device takes ~35s for 20k files — so this returns
    immediately and the row picks up the result on its next poll.
    """
    app = state(request)
    device = app.devices.device(device_id)
    if device is None:
        return HTMLResponse("", status_code=404)
    started = app.scanner.start(device)
    ctx = base_context(request, "devices")
    ctx.update({"device": device, "started": started})
    return templates.TemplateResponse(request, "fragments/scan_started.html", ctx)


@router.get("/device/{device_id}/extras", response_class=HTMLResponse)
async def device_extras(request: Request, device_id: str):
    """Files the device holds that the library does not.

    Orphans: books deleted from the library since, and copies whose filenames were
    mangled by whatever wrote them — a real device turned out to hold 17 of these, the
    same albums a second time under a double-encoded name.

    Answerable only for a scanned device, and the dialog says so rather than reporting
    nought — see `Manifests.extras`. This route is also its own poller while a scan
    started from the dialog runs, so both guards below are checked before anything
    expensive: `all_file_paths()` is 20,782 rows on the Pi and would run every 3s.
    """
    app = state(request)
    device = app.devices.device(device_id)
    if device is None:
        return HTMLResponse("", status_code=404)

    # An unbuilt index answers `all_file_paths()` with an empty set rather than an error,
    # which would report every file on a scanned device as an extra. Unknown, not 24,616.
    scanned = app.manifests.scanned_at(device_id)
    found = (
        app.manifests.extras(
            device_id,
            app.index.all_file_paths(),
            # A mirror is deliberately sent the infrastructure the index does not hold, so
            # on one of those these names are not orphans. See Manifests.extras.
            expected_toplevel=SKIP_TOPLEVEL if device.is_mirror else frozenset(),
        )
        if scanned is not None and app.index.meta().ready
        else Extras.unknown()
    )
    ctx = base_context(request, "devices")
    ctx.update(
        {
            "device": device,
            "extras": found,
            "scan": app.scanner.result(device_id),
            "scanning": app.scanner.is_running(device_id),
            # So the Scan button here shows what it will run, like every action does.
            "commands": {"scan": _shell(scan_argv(device, app.settings))},
        }
    )
    return templates.TemplateResponse(request, "dialogs/device_extras.html", ctx)


@router.get("/device/{device_id}/scan-status", response_class=HTMLResponse)
async def device_scan_status(request: Request, device_id: str):
    app = state(request)
    files, total_bytes, _last = app.manifests.summary(device_id)
    ctx = base_context(request, "devices")
    ctx.update(
        {
            "device_id": device_id,
            "scan": app.scanner.result(device_id),
            "scanning": app.scanner.is_running(device_id),
            "manifest_files": files,
            "manifest_bytes": total_bytes,
        }
    )
    return templates.TemplateResponse(request, "fragments/scan_status.html", ctx)


@router.post("/device/{device_id}/adopt", response_class=HTMLResponse)
async def device_adopt(request: Request, device_id: str):
    """Reconcile a device that already holds the library, without moving its bytes.

    The files are there and correct; only their timestamps say otherwise, so rsync's
    default check would re-send all of them. This queues a `--size-only` run, which
    repairs the metadata and transfers nothing.
    """
    app = state(request)
    device = app.devices.device(device_id)
    if device is None:
        return HTMLResponse("", status_code=404)
    sources = _whole_root_sources(app, device)
    reachable = app.probe.status(device_id).online
    job = app.jobs.submit(
        device,
        sources,
        label="(adopt existing copy)",
        deferred=not reachable,
        adopt=True,
    )
    ctx = base_context(request, "devices")
    ctx["job"] = job
    return templates.TemplateResponse(request, "fragments/queued.html", ctx)


@router.post("/device/{device_id}/dry-run", response_class=HTMLResponse)
async def device_dry_run(request: Request, device_id: str):
    """What a Full Sync would actually do, without doing it.

    Runs as an ordinary job so it queues behind anything in flight, streams its file
    list into the dock, and lands in history — a preview you can read afterwards rather
    than a number that flashes past.
    """
    app = state(request)
    device = app.devices.device(device_id)
    if device is None:
        return HTMLResponse("", status_code=404)
    label = (
        "(dry run · whole root)" if device.is_mirror else "(dry run · full library)"
    )
    job = app.jobs.submit(
        device,
        _whole_root_sources(app, device),
        label=label,
        dry_run=True,
    )
    ctx = base_context(request, "devices")
    ctx["job"] = job
    return templates.TemplateResponse(request, "fragments/queued.html", ctx)


@router.post("/device/{device_id}/full-sync", response_class=HTMLResponse)
async def device_full_sync(request: Request, device_id: str):
    """Queue the whole library. Only offered for devices with `full_sync: true`.

    Not for a mirror node: that one replicates, and the difference is `--delete`. Routing
    it here would hand it Full Sync's "never deletes anything" promise under an action that
    breaks it, so it 404s and `/replicate` is the way in.
    """
    app = state(request)
    device = app.devices.device(device_id)
    if device is None or not device.full_sync or device.is_mirror:
        return HTMLResponse("", status_code=404)
    sources = full_sync_sources(app.settings)
    reachable = app.probe.status(device_id).online
    job = app.jobs.submit(
        device, sources, label="(full library)", deferred=not reachable
    )
    ctx = base_context(request, "devices")
    ctx["job"] = job
    ctx["node"] = _one(request, device_id)
    return templates.TemplateResponse(request, "fragments/queued.html", ctx)


@router.post("/device/{device_id}/replicate", response_class=HTMLResponse)
async def device_replicate(request: Request, device_id: str):
    """Replicate the whole root verbatim. Only for `sync_mode: mirror`.

    Not gated on `full_sync` as well, though it is the larger transfer of the two. That
    flag says "this node can hold the whole library", and a node declared a mirror has
    already said so more strongly — requiring both would let a one-word omission in
    devices.yaml silently hide the only action a mirror node has.
    """
    app = state(request)
    device = app.devices.device(device_id)
    if device is None or not device.is_mirror:
        return HTMLResponse("", status_code=404)
    ctx = base_context(request, "devices")
    reachable = app.probe.status(device_id).online
    try:
        job = app.jobs.submit(
            device,
            _whole_root_sources(app, device),
            label="(replicate · whole root)",
            deferred=not reachable,
        )
    except ValueError as exc:
        # build_argv refused: no sources, or a target at the root. Both are only unsafe
        # because this run carries --delete, so say so instead of queueing it.
        ctx["message"] = str(exc)
        return templates.TemplateResponse(
            request, "fragments/error_toast.html", ctx, status_code=409
        )
    ctx["job"] = job
    ctx["node"] = _one(request, device_id)
    return templates.TemplateResponse(request, "fragments/queued.html", ctx)
