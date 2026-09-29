"""Jobs view, push endpoints, and the multiplexed SSE progress stream."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sse_starlette.sse import EventSourceResponse

from ..deps import base_context, short_path, state
from ..host import host_stats
from ..jobs import JobEvent, full_sync_sources, mirror_sources
from ..state import AppState
from ..templating import templates

router = APIRouter()


def render(name: str, ctx: dict) -> str:
    """Render a fragment outside the request cycle (SSE payloads are HTML)."""
    return templates.env.get_template(name).render(**ctx)


def dock_context(app: AppState) -> dict:
    cards = app.jobs.active() + app.jobs.settled()
    running, pending = app.jobs.counts()
    return {
        "jobs": cards,
        "active_id": cards[-1].id if cards else None,
        "running": running,
        "pending": pending,
        "by_id": app.devices.config.by_id,
        "terminal": app.jobs.terminal,
        "short_path": short_path,
        "total_pct": (sum(j.pct for j in cards) / len(cards)) if cards else 0.0,
    }


def source_label(app: AppState):
    """Build the Jobs table's SOURCE renderer: a selection of every top-level directory is
    the library, and says `/Books`. A closure, so the one scandir happens per render."""
    root = str(app.settings.library_root)
    whole = set(full_sync_sources(app.settings))

    def render(job) -> str:
        # A pull's source is the far end.
        if getattr(job, "kind", "push") == "pull":
            device = app.devices.config.by_id.get(job.device_id)
            if device is not None:
                target = device.target.rstrip("/")
                return f"{device.effective_user}@{device.host}:{target}/"
        sources = [s for s in job.sources if s]
        # No sources, or every top-level directory: either way, the library itself.
        if not sources or (whole and set(sources) >= whole):
            return root
        if len(sources) == 1:
            return f"{root}/{sources[0]}"
        return f"{root}/{sources[0]} +{len(sources) - 1}"

    return render


def jobs_context(request: Request) -> dict:
    app = state(request)
    running, pending = app.jobs.counts()
    ctx = base_context(request, "jobs")
    ctx.update(
        {
            "jobs": app.jobs.recent(),
            "running": running,
            "pending": pending,
            "by_id": app.devices.config.by_id,
            "host": host_stats(app.settings.library_root),
            "concurrency": app.settings.concurrency,
            "defaults": app.devices.config.defaults,
            "source_label": source_label(app),
        }
    )
    return ctx


# ------------------------------------------------------------------ pages --


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    return templates.TemplateResponse(request, "jobs.html", jobs_context(request))


@router.get("/jobs/rows", response_class=HTMLResponse)
async def jobs_rows(request: Request):
    return templates.TemplateResponse(request, "job_rows.html", jobs_context(request))


@router.get("/jobs/dock", response_class=HTMLResponse)
async def dock(request: Request, active: int | None = None):
    """Dock body. `active` switches tabs; the SSE `dock` event uses the same template."""
    app = state(request)
    ctx = dock_context(app)
    if active is not None and any(j.id == active for j in ctx["jobs"]):
        ctx["active_id"] = active
    return templates.TemplateResponse(request, "dock.html", ctx)


@router.get("/jobs/telemetry", response_class=HTMLResponse)
async def jobs_telemetry(request: Request):
    """Three numbers, every 3 s while a job runs: its own context, not `jobs_context`."""
    app = state(request)
    running, _pending = app.jobs.counts()
    return templates.TemplateResponse(
        request,
        "fragments/telemetry.html",
        {"host": host_stats(app.settings.library_root), "running": running},
    )


# ------------------------------------------------------------- submission --


def _resolve(app: AppState, paths: list[str]) -> list[str]:
    """Keep only paths the index vouches for. The index is the whitelist."""
    out = []
    for raw in paths:
        entry = app.index.entry(raw)
        if entry is not None:
            out.append(entry.path)
    return out


def _submit(
    app: AppState, device, paths: list[str], *, deferred=False, dry_run=False, hold=False
):
    label = short_path(paths[0]) + (f" +{len(paths) - 1}" if len(paths) > 1 else "")
    return app.jobs.submit(
        device, paths, label=label, deferred=deferred, dry_run=dry_run, hold=hold
    )


def _queue(
    app: AppState,
    device_id: str,
    paths: list[str],
    dry_run: bool = False,
    full_library: bool = False,
):
    device = app.devices.config.by_id.get(device_id)
    if device is None:
        return None, "unknown device"
    if device.is_upstream:
        # Refused, not re-derived: a pull has its own route.
        return None, f"{device.name} is an upstream source — it is pulled from, never pushed to"
    if device.is_mirror:
        # Re-derived whole: `_resolve` would strip the unindexed vault while --delete stayed.
        try:
            return (
                app.jobs.submit(
                    device,
                    mirror_sources(app.settings),
                    label=(
                        "(dry run · whole root)"
                        if dry_run
                        else "(replicate · whole root)"
                    ),
                    deferred=not app.probe.status(device.id).online and not dry_run,
                    dry_run=dry_run,
                ),
                None,
            )
        except ValueError as exc:
            return None, str(exc)
    if full_library and device.full_sync:
        # A Full Sync, re-derived too, so "full library" still means the current library.
        try:
            return (
                app.jobs.submit(
                    device,
                    full_sync_sources(app.settings),
                    label="(dry run · full library)" if dry_run else "(full library)",
                    deferred=not app.probe.status(device.id).online and not dry_run,
                    dry_run=dry_run,
                    whole_library=True,
                ),
                None,
            )
        except ValueError as exc:
            return None, str(exc)
    wanted = _resolve(app, paths)
    if not wanted:
        return None, "nothing selected"
    reachable = app.probe.status(device.id).online
    return (
        _submit(
            app,
            device,
            wanted,
            deferred=not reachable and not dry_run,
            dry_run=dry_run,
        ),
        None,
    )


def _selection(app: AppState, device_ids: list[str], paths: list[str]):
    """`(targets, paths, error)` for a push or a dry run of a Library selection.

    One gate for both routes, because a form post is not limited to what the picker drew:
    `/jobs/dry-run` once had no mode check at all, so an upstream target was an unhandled
    500 and a mirror queued a whole-root `--delete -n` under a subtree label. The two
    refusals stay separate because their remedies differ -- a mirror takes the whole root
    from its own Replicate, an upstream takes nothing.
    """
    targets = [d for d in (app.devices.config.by_id.get(x) for x in device_ids) if d]
    upstreams = [d.name for d in targets if d.is_upstream]
    if upstreams:
        return [], [], (
            f"{', '.join(upstreams)}: an upstream source — it is pulled from, never pushed to"
        )
    mirrors = [d.name for d in targets if d.is_mirror]
    if mirrors:
        return [], [], (
            f"{', '.join(mirrors)}: a mirror node replicates the whole root — use Replicate"
        )
    if not targets:
        return [], [], "no device selected"
    wanted = _resolve(app, paths)
    if not wanted:
        return [], [], "nothing selected"
    return targets, wanted, None


@router.post("/jobs", response_class=HTMLResponse)
async def create_job(
    request: Request,
    device: list[str] = Form(default=[]),
    path: list[str] = Form(default=[]),
    confirmed: str = Form(""),
    auto: str = Form(""),
):
    """Queue a push, one job per selected device. An unreachable device gets the
    confirmation dialog first, and nothing is created until the user confirms."""
    app = state(request)
    ctx = base_context(request, "library")
    targets, wanted, error = _selection(app, device, path)
    if error:
        ctx["message"] = error
        return templates.TemplateResponse(request, "fragments/error_toast.html", ctx)

    # Ask about the first unreachable device before creating anything at all.
    if confirmed != "yes":
        for target in targets:
            if not app.probe.status(target.id).online:
                ctx.update(
                    {
                        "device": target,
                        "others": [t for t in targets if t is not target],
                        "paths": wanted,
                        "reach": app.probe.status(target.id),
                    }
                )
                return templates.TemplateResponse(
                    request, "dialogs/offline_push.html", ctx
                )

    # "auto" absent on a confirmed push: the user unticked it and wants the job held.
    hold = confirmed == "yes" and auto != "on"
    jobs = []
    for target in targets:
        reachable = app.probe.status(target.id).online
        jobs.append(
            _submit(app, target, wanted, deferred=not reachable, hold=hold)
        )
    ctx["jobs_queued"] = jobs
    return templates.TemplateResponse(request, "fragments/queued.html", ctx)


@router.post("/jobs/dry-run", response_class=HTMLResponse)
async def dry_run(
    request: Request,
    device: list[str] = Form(default=[]),
    path: list[str] = Form(default=[]),
):
    """`rsync -n` against each chosen device, without asking about reachability."""
    app = state(request)
    ctx = base_context(request, "library")
    targets, wanted, error = _selection(app, device, path)
    if error:
        ctx["message"] = error
        return templates.TemplateResponse(request, "fragments/error_toast.html", ctx)

    ctx["jobs_queued"] = [
        _submit(app, target, wanted, dry_run=True) for target in targets
    ]
    return templates.TemplateResponse(request, "fragments/queued.html", ctx)


@router.get("/jobs/picker", response_class=HTMLResponse)
async def picker(
    request: Request,
    path: list[str] = Query(default=[]),
    dry_run: bool = False,
):
    """Choose one or more devices for a push. One job per device."""
    app = state(request)
    wanted = _resolve(app, path)
    ctx = base_context(request, "library")
    if not wanted:
        ctx["message"] = "nothing selected"
        return templates.TemplateResponse(request, "fragments/error_toast.html", ctx)

    total = 0
    for raw in wanted:
        entry = app.index.entry(raw)
        if entry is not None:
            total += entry.size

    # For the FAT32 4 GiB warning. See `LibraryIndex.max_file_size`.
    biggest = app.index.max_file_size(wanted)

    ctx.update(
        {
            "paths": wanted,
            "total_bytes": total,
            "biggest": biggest,
            # Only `books` nodes (see `Device.is_selectable`).
            "devices": [d for d in app.devices.config.devices if d.is_selectable],
            "hidden_mirrors": sum(
                1 for d in app.devices.config.devices if d.is_mirror
            ),
            "hidden_upstreams": sum(
                1 for d in app.devices.config.devices if d.is_upstream
            ),
            "dry_run": dry_run,
            "status": app.probe.status,
        }
    )
    return templates.TemplateResponse(request, "dialogs/picker.html", ctx)


# ------------------------------------------------------------------ control --


@router.post("/jobs/{job_id}/abort", response_class=HTMLResponse)
async def abort(request: Request, job_id: int):
    app = state(request)
    await app.jobs.abort(job_id)
    return HTMLResponse("")


@router.post("/jobs/{job_id}/dismiss", response_class=HTMLResponse)
async def dismiss(request: Request, job_id: int):
    """Hide the card. The history row survives; see DELETE for the other meaning."""
    app = state(request)
    app.jobs.dismiss(job_id)
    return templates.TemplateResponse(request, "dock.html", dock_context(app))


@router.post("/jobs/{job_id}/start", response_class=HTMLResponse)
async def start_job(request: Request, job_id: int):
    """Run a held job now, regardless of whether the node has answered yet."""
    app = state(request)
    app.jobs.start_now(job_id)
    return templates.TemplateResponse(request, "job_rows.html", jobs_context(request))


# Above `/jobs/{job_id}`, which would otherwise swallow it as a 422.
@router.delete("/jobs/finished", response_class=HTMLResponse)
async def clear_finished(request: Request):
    app = state(request)
    app.jobs.dismiss_finished()
    app.store.clear_finished()
    return templates.TemplateResponse(request, "job_rows.html", jobs_context(request))


@router.delete("/jobs/{job_id}", response_class=HTMLResponse)
async def delete_job(request: Request, job_id: int):
    """Cancel if live, then remove from history — the Jobs table's ✕."""
    app = state(request)
    await app.jobs.cancel(job_id)
    return templates.TemplateResponse(request, "job_rows.html", jobs_context(request))


@router.post("/jobs/{job_id}/retry", response_class=HTMLResponse)
async def retry(request: Request, job_id: int):
    app = state(request)
    old = app.jobs.get(job_id)
    ctx = base_context(request, "jobs")
    if old is None:
        ctx["message"] = "job not found"
        return templates.TemplateResponse(request, "fragments/error_toast.html", ctx)
    # Repeat what was run, dry run included: a retried preview must stay a preview.
    if getattr(old, "kind", "push") == "pull":
        # Re-derived from the device; a pull takes no sources to go stale.
        device = app.devices.config.by_id.get(old.device_id)
        if device is None or not device.is_upstream:
            ctx["message"] = "that device is no longer an upstream source"
            return templates.TemplateResponse(request, "fragments/error_toast.html", ctx)
        job = app.jobs.submit_pull(
            device,
            deferred=not app.probe.status(device.id).online,
            dry_run=old.dry_run,
        )
        ctx["job"] = job
        return templates.TemplateResponse(request, "fragments/queued.html", ctx)
    job, error = _queue(
        app,
        old.device_id,
        old.sources,
        dry_run=old.dry_run,
        full_library=getattr(old, "full_library", False),
    )
    if job is None:
        ctx["message"] = error
        return templates.TemplateResponse(request, "fragments/error_toast.html", ctx)
    ctx["job"] = job
    return templates.TemplateResponse(request, "fragments/queued.html", ctx)


#: A full-library log is ~400 KB; the dialog shows its end and links the rest.
LOG_TAIL_LINES = 600


@router.get("/jobs/{job_id}/log/view", response_class=HTMLResponse)
async def job_log_view(request: Request, job_id: int):
    """The job's log, in a dialog, next to the job it belongs to."""
    app = state(request)
    job = app.jobs.get(job_id)
    path = app.settings.logs_dir / f"{job_id}.log"

    lines: list[str] = []
    truncated = 0
    size = 0
    if path.exists():
        size = path.stat().st_size
        text = path.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        if len(all_lines) > LOG_TAIL_LINES:
            truncated = len(all_lines) - LOG_TAIL_LINES
            lines = all_lines[-LOG_TAIL_LINES:]
        else:
            lines = all_lines

    ctx = base_context(request, "jobs")
    ctx.update(
        {
            "job": job,
            "job_id": job_id,
            "lines": lines,
            "truncated": truncated,
            "size": size,
            "device": app.devices.config.by_id.get(job.device_id) if job else None,
        }
    )
    return templates.TemplateResponse(request, "dialogs/job_log.html", ctx)


@router.get("/jobs/{job_id}/log", response_class=PlainTextResponse)
async def job_log(request: Request, job_id: int):
    app = state(request)
    path = app.settings.logs_dir / f"{job_id}.log"
    if not path.exists():
        return PlainTextResponse(f"no log for job {job_id}", status_code=404)
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))


# ---------------------------------------------------------------------- SSE --


@router.get("/jobs/stream")
async def stream(request: Request):
    """One multiplexed stream of rendered HTML fragments, which htmx swaps directly."""
    app = state(request)
    queue = app.jobs.subscribe()

    async def publisher():
        try:
            # Paint the current state at once, so a reconnect is never blank.
            yield {
                "event": "dock",
                "data": render("dock.html", dock_context(app)),
                "retry": 3000,
            }
            while True:
                try:
                    event: JobEvent = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    continue
                payload = _render_event(app, event)
                if payload is not None:
                    yield payload
        finally:
            app.jobs.unsubscribe(queue)

    # Never poll request.is_disconnected() here: EventSourceResponse reads the same
    # channel, a second reader steals its messages, and the phantom reconnects fill the
    # six connections a host allows until every page load hangs.
    return EventSourceResponse(publisher(), ping=15)


def _render_event(app: AppState, event: JobEvent) -> dict | None:
    if event.kind == "dock":
        return {"event": "dock", "data": render("dock.html", dock_context(app))}

    job = app.jobs.get(event.job_id) if event.job_id else None
    if job is None:
        return None
    by_id = app.devices.config.by_id

    if event.kind == "progress":
        return {
            "event": f"job-{job.id}-progress",
            "data": render("dock_meta.html", {"job": job, "by_id": by_id}),
        }
    if event.kind == "line":
        return {
            "event": f"job-{job.id}-line",
            "data": render("term_line.html", {"lines": event.lines}),
        }
    if event.kind == "done":
        return {
            "event": f"job-{job.id}-done",
            "data": render(
                "dock_card.html",
                {
                    "job": job,
                    "by_id": by_id,
                    "terminal": app.jobs.terminal,
                    "short_path": short_path,
                    "active_id": job.id,
                },
            ),
        }
    return None
