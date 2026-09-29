"""Devices view: every device in devices.yaml and whether it answers right now."""

from __future__ import annotations

import asyncio
import shlex
import time
from pathlib import Path
from dataclasses import dataclass

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ..deps import base_context, state
from ..probe import (
    Battery,
    FreeSpace,
    Reachability,
    _parse_battery,
    _parse_power,
    _readings_script,
    _section,
    battery_command,
    ssh_argv,
)
from ..config import PULL_EXCLUDES, SKIP_TOPLEVEL
from ..procs import reap
from ..scan import scan_argv
from ..jobs import (
    Job,
    REPLICATE_SUFFIX,
    build_argv,
    catalog_rel,
    remote_reader_argv,
    remote_sidecar_argv,
    replicate_catalog_argv,
    build_catalog_argv,
    build_pull_argv,
    cleanup_argv,
    full_sync_sources,
    hints_for_text,
    mirror_sources,
    service_argv,
    snapshot_argv,
)
from ..manifests import Extras
from ..models import Device
from ..state import AppState
from ..templating import clock, reltime, templates, until

router = APIRouter()


#: What a failed connect implies, keyed on `probe._describe`'s strings. `sleeping` carries no
#: diagnosis of its own, so any reading of one comes from the error.
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


#: TABLE or GRID, per browser. A cookie, because the branch is chosen server-side:
#: localStorage would paint TABLE and swap after load.
VIEW_COOKIE = "libnodes_view"
VIEW_MAX_AGE = 31536000


def resolved_view(request: Request, view: str | None = None) -> str:
    """Which layout this browser is on: an explicit `?view=`, else the cookie, which is
    only ever written from an explicit one and so always matches the branch rendered."""
    if view in ("table", "grid"):
        return view
    return "grid" if request.cookies.get(VIEW_COOKIE) == "grid" else "table"


@dataclass
class DeviceView:
    """One device row: config, live reachability, storage, and any running transfer."""

    device: Device
    reach: Reachability
    space: FreeSpace
    battery: Battery
    last_sync: float | None
    job: Job | None
    #: The connection test as a shell line, for the Test button's tooltip; the result
    #: echoes the same line, both from `_test_argv`.
    test_command: str = ""
    #: A scan is running. It is not a Job and never reaches the dock, so without this the
    #: row gave no sign of it once its dialog closed.
    scanning: bool = False

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
        """Green, which every writing action needs."""
        return self.state == "online"

    @property
    def reach_note(self) -> str:
        """A failed row's tooltip: what the error suggests, when the node last answered,
        and how old the check is -- the row repaints every 10 s but the probe behind it
        backs off, so without the age a dot claims a measurement it did not just take."""
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
        """What the Storage column prints, because it is what the bar draws."""
        return self.space.used

    @property
    def used_pct(self) -> float:
        total = self.capacity
        if not total or self.space.used is None:
            return 0.0
        return max(0.0, min(100.0, 100.0 * self.space.used / total))

    @property
    def has_battery(self) -> bool:
        """Whether a battery source is declared -- not whether one was read, or the cell
        would come and go with the poll."""
        return bool(self.device.battery or self.device.battery_cmd)

    @property
    def battery_source(self) -> str:
        """The file or command the reading came from, named in the tooltip."""
        return self.device.battery or self.device.battery_cmd or ""

    @property
    def battery_pct(self) -> float:
        return float(self.battery.percent or 0)

    @property
    def battery_class(self) -> str:
        """The bar's tint, low-is-bad at the levels a phone warns at (storage is the
        opposite, so the two cannot share a threshold)."""
        pct = self.battery.percent
        if pct is None:
            return ""
        if pct <= 15:
            return "track-err"
        if pct <= 30:
            return "track-warn"
        return ""

    @property
    def bolt_class(self) -> str:
        """The charging glyph's tint -- amber charging, green plugged and full -- or "".

        None on a red row: an unreachable device produces no read to blank a stale bolt
        with, and s4l sat five days at `100%` beside a charger nothing could ask about.
        Red means the reading is at least `sleeping_window` old; amber keeps its bolt.
        """
        if self.offline:
            return ""
        if self.battery.power == "charging":
            return "bolt-charging"
        if self.battery.power == "plugged":
            return "bolt-plugged"
        return ""

    @property
    def battery_note(self) -> str:
        """The cell's tooltip: the reading, its age, whether it is on a charger, and why
        it is missing if it is."""
        if self.battery.error:
            stale = (
                f"last read {reltime(self.battery.checked_at)}"
                if self.battery.checked_at
                else "never read"
            )
            return f"{self.battery_source}: {self.battery.error} · {stale}"
        if self.battery.percent is None:
            return f"{self.battery_source} — not read yet"
        # Spelled out, because the bolt cannot tell "on battery" from "charger unread".
        # Past tense on a red row, which has withdrawn the bolt.
        power = {
            "charging": "charging",
            "plugged": "on charger, not charging",
            "unplugged": "on battery",
        }.get(self.battery.power or "", "")
        if power:
            power = f" · was {power}" if self.offline else f" · {power}"
        return (
            f"{self.battery.percent}%{power} · "
            f"read {reltime(self.battery.checked_at)}"
        )

    @property
    def seen_at(self) -> float | None:
        """When this row's readings were taken -- the LAST SEEN column. One ssh carries df
        and battery, so one stamp dates both. Not `reach.last_ok`, the connect, which is up
        to `freespace_interval` fresher than the figures."""
        return self.space.checked_at or self.battery.checked_at

    @property
    def seen_note(self) -> str:
        """LAST SEEN's tooltip, both cadences: the readings and the connect disagree as a
        matter of course."""
        if self.seen_at is None:
            return "no reading yet"
        sources = "df + battery" if self.has_battery else "df"
        parts = [f"{sources} read at {clock(self.seen_at)}"]
        # The storage cell has no room for why a reading failed.
        if self.space.error:
            parts.append(self.space.error)
        parts.append(
            f"answered {reltime(self.reach.last_ok)}"
            if self.reach.last_ok
            else "has never answered"
        )
        return " · ".join(parts)


def _running(app: AppState) -> dict[str, Job]:
    return {j.device_id: j for j in app.jobs.active() if j.state == "running"}


def _view(app: AppState, device: Device, running: dict[str, Job]) -> DeviceView:
    return DeviceView(
        device=device,
        reach=app.probe.status(device.id),
        space=app.probe.space(device.id),
        battery=app.probe.battery(device.id),
        last_sync=app.manifests.last_sync(device.id),
        job=running.get(device.id),
        scanning=app.scanner.is_running(device.id),
        test_command=shlex.join(_test_argv(device, app.settings)),
    )


def device_views(app: AppState) -> list[DeviceView]:
    running = _running(app)
    return [_view(app, device, running) for device in app.devices.config.devices]


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


def devices_context(
    request: Request, q: str | None = None, view: str | None = None
) -> dict:
    app = state(request)
    # A stamp, not a probe: it tightens the backoff ceiling while a page polls.
    app.probe.note_interest()
    ctx = _status_context(request)
    ctx.update(
        {
            "nodes": _filtered(device_views(app), q),
            "q": q or "",
            "view": resolved_view(request, view),
        }
    )
    return ctx


def _status_context(request: Request) -> dict:
    """What the top-bar chips need, and not the rows: `/devices/status` polls every 10 s
    beside `/devices/rows`, and building every row for it doubled the poll's cost."""
    app = state(request)
    online, total = app.probe.reachable_count
    ctx = base_context(request, "devices")
    ctx.update(
        {
            "online": online,
            "total": total,
            "last_scan": app.probe.last_scan,
            "profiles": app.devices.config.profiles,
            # devices.yaml's only error surface: the last good config keeps serving.
            "issues": app.devices.issues,
            # The titlebar subtitle rides out of band only on /devices/status; inline it
            # would be a duplicate id.
            "oob": False,
        }
    )
    return ctx


@router.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request, q: str | None = None, view: str | None = None):
    """The Devices page, in this browser's last layout. Only an explicit `?view=` writes
    the cookie: the rail's bare /devices must not pin the default it guessed."""
    ctx = devices_context(request, q, view)
    response = templates.TemplateResponse(request, "devices.html", ctx)
    if view in ("table", "grid"):
        response.set_cookie(
            VIEW_COOKIE,
            view,
            # No `secure`: plain http on the LAN would never store it.
            httponly=True,
            samesite="lax",
            path="/",
            max_age=VIEW_MAX_AGE,
        )
    return response


@router.get("/devices/rows", response_class=HTMLResponse)
async def device_rows(request: Request, q: str | None = None):
    return templates.TemplateResponse(request, "device_rows.html", devices_context(request, q))


@router.get("/devices/grid", response_class=HTMLResponse)
async def device_grid(request: Request, q: str | None = None):
    return templates.TemplateResponse(request, "device_grid.html", devices_context(request, q))


@router.get("/devices/status", response_class=HTMLResponse)
async def device_status(request: Request):
    """The top-bar chips, polled beside the table, with the titlebar subtitle out of band
    -- the one figure no container repaints."""
    app = state(request)
    app.probe.note_interest()
    ctx = _status_context(request)
    ctx["oob"] = True
    return templates.TemplateResponse(request, "device_status.html", ctx)


def _one(request: Request, device_id: str) -> DeviceView | None:
    app = state(request)
    device = app.devices.device(device_id)
    return None if device is None else _view(app, device, _running(app))


@router.get("/device/{device_id}/row", response_class=HTMLResponse)
async def device_row(request: Request, device_id: str):
    view = _one(request, device_id)
    if view is None:
        return HTMLResponse("", status_code=404)
    ctx = base_context(request, "devices")
    ctx["node"] = view
    return templates.TemplateResponse(request, "device_row.html", ctx)


@router.get("/device/{device_id}/card", response_class=HTMLResponse)
async def device_card(request: Request, device_id: str):
    """The grid's answer to /device/{id}/row -- one card, swapped as outerHTML."""
    node = _one(request, device_id)
    if node is None:
        return HTMLResponse("", status_code=404)
    ctx = base_context(request, "devices")
    ctx["node"] = node
    return templates.TemplateResponse(request, "device_card.html", ctx)


@router.post("/devices/rescan", response_class=HTMLResponse)
async def devices_rescan(request: Request, q: str | None = Form(default=None)):
    """Probe every device, ignoring backoff, and rebuild the index, without waiting for
    either; the fragment schedules one follow-up refresh.

    `q` is a Form field: htmx puts an included value in a POST's *body*, and as a query
    parameter it bound to None and Rescan erased the filter.
    """
    app = state(request)
    app.probe.rescan_soon(force=True)
    # One "look again" control for both; the walk is ~1 s on its own thread.
    app.reindex_soon()
    ctx = devices_context(request, q)
    ctx["rescanning"] = True
    template = "device_grid.html" if ctx["view"] == "grid" else "device_rows.html"
    return templates.TemplateResponse(request, template, ctx)


def _preview(build) -> str:
    """A command strip, or why there is none: the builders refuse what they cannot make
    safe, and the dialog shows the refusal rather than failing."""
    try:
        return shlex.join(build())
    except ValueError as exc:
        return f"unavailable — {exc}"


def _pull_plan(app: AppState, device: Device) -> list[str]:
    """Every command a Pull runs, in order, built by the functions the runner calls.

    The rsync alone would hide the steps that carry the risk: its --delete (with the cap
    beside it) and the `systemctl stop` with an overwritten catalog behind it.
    """
    config = app.devices.config
    steps: list[str] = []

    def step(n: int, what: str, build) -> None:
        try:
            steps.append(f"{n}. {what}\n   {shlex.join(build())}")
        except ValueError as exc:
            steps.append(f"{n}. {what}\n   unavailable — {exc}")

    unit = app.settings.local_service
    if unit:
        # Numbered 0 so the six phases keep the numbers the dock prints for them.
        steps.append(
            "0. before anything moves: is this host's reader running, and may we manage "
            "it? (the start is a no-op on a running unit)\n   "
            f"{shlex.join(service_argv('is-active', app.settings))}\n   "
            f"{shlex.join(service_argv('start', app.settings))}"
        )
    step(1, "the library — and prune what this node no longer has, both services still running",
         lambda: build_pull_argv(device, config, app.settings))
    step(2, "snapshot the upstream's catalog, without stopping it",
         lambda: snapshot_argv(device, config, app.settings))
    if unit:
        steps.append(
            "3. stop this host's reader, if it is still running\n   "
            f"{shlex.join(service_argv('stop', app.settings))}"
        )
    else:
        steps.append(
            "3. stop this host's reader\n   unavailable — no LIBNODES_LOCAL_SERVICE "
            "declared, so the catalog phase is skipped entirely"
        )
    step(4, "swap the catalog in", lambda: build_catalog_argv(device, config, app.settings))
    if unit:
        steps.append(
            "5. start it again if step 3 stopped it, whatever happened between\n   "
            f"{shlex.join(service_argv('start', app.settings))}"
        )
    else:
        # Listed even when it cannot run, so the numbering never skips.
        steps.append(
            "5. start it again, whatever happened\n   unavailable — nothing was stopped"
        )
    step(6, "remove the snapshot from the upstream",
         lambda: cleanup_argv(device, config, app.settings))
    return steps


def _replicate_plan(app: AppState, device: Device, sources: list[str]) -> list[str]:
    """Replicate's commands: the files, then the catalog steps worth reading first."""
    config = app.devices.config
    try:
        steps = [
            "1. the library, --delete and all\n   "
            + shlex.join(build_argv(device, config, sources, app.settings))
        ]
    except ValueError as exc:
        return [f"unavailable — {exc}"]

    if catalog_rel(app.settings) is None or not Path(app.settings.catalog_db).exists():
        return steps
    if app.settings.local_service:
        steps.append(
            "2. is anything reading the catalog there?\n   "
            + shlex.join(remote_reader_argv(device, config, app.settings))
        )
    steps.append(
        "3. snapshot ours, with this host's own reader still serving\n   "
        f"sqlite3 .backup {app.settings.catalog_db} "
        f"-> {app.settings.catalog_db}{REPLICATE_SUFFIX}"
    )
    steps.append(
        "4. clear the replica's stale write-ahead log\n   "
        + shlex.join(remote_sidecar_argv(device, config, app.settings))
    )
    steps.append(
        "5. send the snapshot in as its lib.db\n   "
        + shlex.join(replicate_catalog_argv(device, config, app.settings))
    )
    return steps


def _whole_root_sources(app: AppState, device: Device) -> list[str]:
    """"The whole library" for this device's mode: a reader's browsable categories, or a
    CAS node's entire root, vault included."""
    if device.cas_tree:
        return mirror_sources(app.settings)
    return full_sync_sources(app.settings)


@router.get("/device/{device_id}/menu", response_class=HTMLResponse)
async def device_menu(request: Request, device_id: str):
    """Every action for one device, each showing the command it will run: "Adopt existing
    copy" means nothing until you see its `--size-only`."""
    app = state(request)
    device = app.devices.device(device_id)
    if device is None:
        return HTMLResponse("", status_code=404)

    config = app.devices.config
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
                # `whole_library=True`, so a `prune: true` node's preview shows the
                # --delete its Full Sync will carry; the note beside it in
                # device_menu.html changes with `device.prune` to match. Replicate's
                # commands are `replicate_plan`, below.
                "full_sync": _preview(
                    lambda: build_argv(
                        device, config, sources, app.settings, whole_library=True
                    )
                ),
                "dry_run": _preview(
                    lambda: build_argv(
                        device,
                        config,
                        sources,
                        app.settings,
                        dry_run=True,
                        whole_library=True,
                    )
                ),
                "adopt": _preview(
                    lambda: build_argv(device, config, sources, app.settings, adopt=True)
                ),
                "scan": shlex.join(scan_argv(device, app.settings)),
                "pull_dry_run": _preview(
                    lambda: build_pull_argv(
                        device, config, app.settings, dry_run=True
                    )
                ) if device.is_upstream else "",
            },
            "pull_plan": _pull_plan(app, device) if device.is_upstream else [],
            "replicate_plan": (
                _replicate_plan(app, device, sources) if device.is_mirror else []
            ),
            "library_root": str(app.settings.library_root),
        }
    )
    return templates.TemplateResponse(request, "dialogs/device_menu.html", ctx)


#: The connection test's own questions, after the readings the background probe takes:
#: does the device have rsync, and is the target writable. Read-only -- `test -w` rather
#: than creating a probe file on someone's device.
_TEST_TAIL = (
    'echo "# rsync"; rsync --version 2>/dev/null | head -1 || echo "rsync: not found"; '
    'echo "# write"; if test -w {t}; then echo "writable"; else echo "NOT writable"; fi'
)


def _test_script(device: Device) -> str:
    """The background probe's readings -- df, battery, charger -- plus the test's own two.

    Built from `_readings_script` rather than beside it, so pressing Test cannot read a
    different set of things than the poll does. The tail is formatted on its own: a
    `battery_cmd` is free-form shell and may contain braces -- `awk '{print $1}'` -- which
    str.format would read as a field name.
    """
    return f"{_readings_script(device)}; " + _TEST_TAIL.format(t=shlex.quote(device.target))


def _test_argv(device: Device, settings) -> list[str]:
    """The connection test as one argv: the tooltip, the echoed line and the run agree."""
    return [
        *ssh_argv(device, settings),
        _test_script(device),
    ]


@router.post("/device/{device_id}/test", response_class=HTMLResponse)
async def device_test(request: Request, device_id: str):
    """ssh in, read without writing, and report in a dialog: the command, the output, a
    one-line verdict and a likely cause. On the row rather than behind Actions, because
    it writes nothing and is most useful when the device is not answering."""
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
    # Re-probe and keep the readings just taken, so the row refreshed out of band below
    # agrees with the dialog rather than showing the last poll's figures.
    await app.probe.probe(device)
    app.probe.adopt_space(device_id, _section(out, "df"))
    if battery_command(device):
        app.probe.adopt_battery(
            device_id, _section(out, "battery"), _section(out, "power")
        )

    ctx = base_context(request, "devices")
    ctx.update(
        {
            "device": device,
            "command": shlex.join(argv),
            "stdout": out.strip(),
            "stderr": err.strip(),
            "code": code,
            "elapsed": elapsed,
            "summary": _test_summary(out) if code == 0 else None,
            "hints": hints_for_text(f"{out}\n{err}", code if code is not None else 255),
            "node": _one(request, device_id),
            "oob": True,
            # Row or card: aimed at a `#node-…` grid mode never draws, htmx drops the swap.
            "view": resolved_view(request),
        }
    )
    return templates.TemplateResponse(request, "dialogs/test_result.html", ctx)


#: The charge state as the verdict words it.
_POWER_VERDICT = {
    "charging": " (charging)",
    "plugged": " (on charger)",
    "unplugged": "",
}


def _test_summary(out: str) -> list[str]:
    """Turn the probe's output into the design's one-line verdict."""
    bits = []
    if "# df" in out:
        bits.append("reachable")
    # Parsed, not echoed, so the verdict claims the figure the row's bar draws.
    charge = _parse_battery(_section(out, "battery"))
    if charge is not None:
        # `.get` with a default, or an unreadable charger reads "battery 100%None".
        power = _POWER_VERDICT.get(
            _parse_power(_section(out, "power") or _section(out, "battery")) or "", ""
        )
        bits.append(f"battery {charge}%{power}")
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
    """Ask the device what it holds, in the background (~35 s for 20k files); the row
    shows SCANNING until it finishes."""
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
    """Files the device holds that the library does not: retired books, and copies under
    mangled names. Answerable only after a scan (see `Manifests.extras`). The dialog polls
    this route while its scan runs, so the cheap guards come first."""
    app = state(request)
    device = app.devices.device(device_id)
    if device is None:
        return HTMLResponse("", status_code=404)

    # An unbuilt index would make every file an extra. Unknown instead.
    scanned = app.manifests.scanned_at(device_id)
    found = (
        app.manifests.extras(
            device_id,
            app.index.all_file_paths(),
            expected_toplevel=SKIP_TOPLEVEL if device.cas_tree else frozenset(),
        )
        if scanned is not None and app.index.meta().ready
        else Extras.unknown()
    )
    # On an upstream this is the pull backlog, and the pull's excludes decline most of it
    # (/Unsorted/ was 56 of the 56.5 GB listed). Marked per row, not hidden.
    pull_held = 0
    if device.is_upstream:
        patterns = [
            p.strip("/") for p in device.pull_excludes_with(PULL_EXCLUDES)
        ]
        for row in found.rows:
            path = row["path"]
            row["held_back"] = any(
                path == pat or path.startswith(pat + "/") for pat in patterns
            )
            if row["held_back"]:
                pull_held += 1

    ctx = base_context(request, "devices")
    ctx.update(
        {
            "device": device,
            "extras": found,
            "pull_held": pull_held,
            "scan": app.scanner.result(device_id),
            "scanning": app.scanner.is_running(device_id),
            "commands": {"scan": shlex.join(scan_argv(device, app.settings))},
            "pull_plan": _pull_plan(app, device) if device.is_upstream else [],
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


def _queue_toast(request: Request, device_id: str, offered, submit) -> HTMLResponse:
    """One device action: look the node up, refuse a mode it is not offered to, queue the
    job, and answer with a toast.

    `offered(device)` is the route's mode guard -- a 404, as for an unknown id. `submit(app,
    device, reachable)` queues the job; a ValueError from it is `build_argv` refusing a
    transfer it cannot make safe (no sources, a target at the root, an upstream), and is a
    409 toast that says why rather than a 500.
    """
    app = state(request)
    device = app.devices.device(device_id)
    if device is None or not offered(device):
        return HTMLResponse("", status_code=404)
    ctx = base_context(request, "devices")
    try:
        ctx["job"] = submit(app, device, app.probe.status(device_id).online)
    except ValueError as exc:
        ctx["message"] = str(exc)
        return templates.TemplateResponse(
            request, "fragments/error_toast.html", ctx, status_code=409
        )
    return templates.TemplateResponse(request, "fragments/queued.html", ctx)


@router.post("/device/{device_id}/adopt", response_class=HTMLResponse)
async def device_adopt(request: Request, device_id: str):
    """Reconcile a device that already holds the library, without moving its bytes.

    The files are there and correct; only their timestamps say otherwise, so rsync's
    default check would re-send all of them. This queues a `--size-only` run, which
    repairs the metadata and transfers nothing. Not for an upstream: --size-only makes a
    push quieter, not read-only, and Adopt was once the last writing route that reached one.
    """
    return _queue_toast(
        request, device_id,
        lambda d: not d.is_upstream,
        lambda app, d, reachable: app.jobs.submit(
            d, _whole_root_sources(app, d), label="(adopt existing copy)",
            deferred=not reachable, adopt=True,
        ),
    )


@router.post("/device/{device_id}/dry-run", response_class=HTMLResponse)
async def device_dry_run(request: Request, device_id: str):
    """What a Full Sync or a Replicate would actually do, without doing it.

    Runs as an ordinary job so it queues behind anything in flight, streams its file list
    into the dock and lands in history. `whole_library=True`, because the sources *are*
    the library: the preview must carry whatever the real run would, a `prune: true`
    node's --delete included -- `-n` is what makes that safe. An upstream previews a
    *pull*, at /pull-dry-run.
    """
    return _queue_toast(
        request, device_id,
        lambda d: not d.is_upstream,
        lambda app, d, reachable: app.jobs.submit(
            d, _whole_root_sources(app, d),
            label="(dry run · whole root)" if d.is_mirror else "(dry run · full library)",
            dry_run=True, whole_library=True,
        ),
    )


@router.post("/device/{device_id}/full-sync", response_class=HTMLResponse)
async def device_full_sync(request: Request, device_id: str):
    """Queue the whole library. Only for a `books` node with `full_sync: true`.

    Not for a mirror, whose transfer is defined by --delete and which replicates instead,
    and not for an upstream -- the explicit term is what keeps Full Sync, a push to
    production, out of reach the instant a node stops being a mirror. `whole_library` is
    the precondition `Device.prune` needs before build_argv will add --delete.
    """
    return _queue_toast(
        request, device_id,
        lambda d: d.full_sync and not d.is_mirror and not d.is_upstream,
        lambda app, d, reachable: app.jobs.submit(
            d, full_sync_sources(app.settings), label="(full library)",
            deferred=not reachable, whole_library=True,
        ),
    )


@router.post("/device/{device_id}/replicate", response_class=HTMLResponse)
async def device_replicate(request: Request, device_id: str):
    """Replicate the whole root verbatim. Only for `sync_mode: mirror`.

    Not gated on `full_sync` as well: a node declared a mirror has already said it holds
    the whole library, and requiring both would let a one-word omission hide its only
    action. `is_mirror` already excludes an upstream, and must not be widened to
    `cas_tree` -- pinned by test_replicate_is_not_a_way_into_an_upstream_node.
    """
    return _queue_toast(
        request, device_id,
        lambda d: d.is_mirror,
        lambda app, d, reachable: app.jobs.submit(
            d, _whole_root_sources(app, d), label="(replicate · whole root)",
            deferred=not reachable,
        ),
    )


@router.post("/device/{device_id}/pull", response_class=HTMLResponse)
async def device_pull(request: Request, device_id: str):
    """Bring the upstream's library here. Only for `sync_mode: upstream`.

    One Job and one dock card for all six phases -- see JobRunner._run_pull -- because the
    `finally` that restarts this host's reader has to span them.
    """
    return _queue_toast(
        request, device_id,
        lambda d: d.is_upstream,
        lambda app, d, reachable: app.jobs.submit_pull(d, deferred=not reachable),
    )


@router.post("/device/{device_id}/pull-dry-run", response_class=HTMLResponse)
async def device_pull_dry_run(request: Request, device_id: str):
    """What a Pull would bring across, and what it would prune, without either.

    Phase 1 with -n and nothing else: no snapshot is written onto the upstream and no
    service is stopped.
    """
    return _queue_toast(
        request, device_id,
        lambda d: d.is_upstream,
        lambda app, d, reachable: app.jobs.submit_pull(
            d, deferred=not reachable, dry_run=True
        ),
    )
