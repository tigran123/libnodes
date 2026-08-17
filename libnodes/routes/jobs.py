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
    """Build the Jobs table's SOURCE renderer.

    A selection covering every top-level directory *is* the library, and saying `/Books`
    beats naming one arbitrary member and counting the rest. The cell used to read
    `/Books/Art +16 (full)`, where `Art` was only alphabetically first and `(full)` was
    `len(sources) > 3` — a heuristic that called any four directories the whole library
    and, on a genuine full push, still printed a directory name it had picked at random.

    Returns a closure so the one scandir behind `full_sync_sources` happens per render
    rather than per row. It is a single level of the root, not a library walk.
    """
    root = str(app.settings.library_root)
    whole = set(full_sync_sources(app.settings))

    def render(job) -> str:
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
    return templates.TemplateResponse(request, "fragments/telemetry.html", jobs_context(request))


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


def _queue(app: AppState, device_id: str, paths: list[str], dry_run: bool = False):
    device = app.devices.config.by_id.get(device_id)
    if device is None:
        return None, "unknown device"
    if device.is_mirror:
        # `_resolve` filters against the index, which by design holds no `.data/` — so a
        # mirror push arriving here would be stripped down to the browsable categories
        # while `build_argv` still added `--delete`, leaving preserved symlinks pointing at
        # a vault that was never sent. Retry is the live route into this: it replays a
        # stored job's sources. Re-derive the whole root instead of narrowing it.
        try:
            return (
                app.jobs.submit(
                    device,
                    mirror_sources(app.settings),
                    # Not `_submit`'s label: that names the first path and a count, which
                    # for a whole-root replica reads as ".data +4".
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


@router.post("/jobs", response_class=HTMLResponse)
async def create_job(
    request: Request,
    device: list[str] = Form(default=[]),
    path: list[str] = Form(default=[]),
    confirmed: str = Form(""),
    auto: str = Form(""),
):
    """Queue a push — one job per selected device.

    If a device is unreachable this returns the confirmation dialog and creates
    **nothing** for it; the job only exists once the user confirms. Creating it first
    and cancelling afterwards left an un-asked-for job in the history.
    """
    app = state(request)
    ctx = base_context(request, "library")

    targets = [d for d in (app.devices.config.by_id.get(x) for x in device) if d]
    # The picker does not offer mirror nodes, but a hidden button is not a guard: this is
    # a form post. A mirror takes the whole root from its own Replicate action, never a
    # selection — see the note in `_queue`.
    mirrors = [d for d in targets if d.is_mirror]
    if mirrors:
        names = ", ".join(d.name for d in mirrors)
        ctx["message"] = f"{names}: a mirror node replicates the whole root — use Replicate"
        return templates.TemplateResponse(request, "fragments/error_toast.html", ctx)
    if not targets:
        ctx["message"] = "no device selected"
        return templates.TemplateResponse(request, "fragments/error_toast.html", ctx)

    wanted = _resolve(app, path)
    if not wanted:
        ctx["message"] = "nothing selected"
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

    # An unchecked box submits nothing, so "auto" absent on a confirmed push means the
    # user deliberately cleared it and wants the job held.
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
    """`rsync -n` against each chosen device. Never asks about reachability: a dry run
    that cannot connect simply fails, and costs nothing."""
    app = state(request)
    ctx = base_context(request, "library")

    targets = [d for d in (app.devices.config.by_id.get(x) for x in device) if d]
    wanted = _resolve(app, path)
    if not targets:
        ctx["message"] = "no device selected"
        return templates.TemplateResponse(request, "fragments/error_toast.html", ctx)
    if not wanted:
        ctx["message"] = "nothing selected"
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

    # FAT32 cannot hold a file of 4 GiB or more. Say so before the transfer fails
    # halfway rather than after. Asked of the index rather than summed from the entries
    # above, because `Entry.size` on a directory is its recursive total: selecting the
    # library's 17 top-level directories claimed a largest file of 68.7 GB — `Science`
    # whole — and warned about a 4 GiB limit the biggest real file (786 MB) is nowhere
    # near.
    biggest = app.index.max_file_size(wanted)

    ctx.update(
        {
            "paths": wanted,
            "total_bytes": total,
            "biggest": biggest,
            # Mirror nodes are not offered: they take the whole root or nothing, and a
            # subtree of preserved symlinks has no vault to resolve against. See
            # library_context, which drops them from the row buttons for the same reason.
            "devices": [d for d in app.devices.config.devices if not d.is_mirror],
            # So the empty case can say *why* it is empty. "No devices configured" would be
            # a lie on a fleet that is all mirrors.
            "hidden_mirrors": sum(
                1 for d in app.devices.config.devices if d.is_mirror
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


# Must stay above `/jobs/{job_id}`: FastAPI matches routes in declaration order, so below
# it this literal is swallowed by the parameterised one and every Clear finished is a 422
# on int("finished") -- a button that silently does nothing.
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
    # Repeat what was run, dry run included. Retrying a preview as a real transfer is
    # wrong in any mode; on a mirror it would turn "show me what would change" into a
    # --delete push, which is the one place it is unrecoverable.
    job, error = _queue(app, old.device_id, old.sources, dry_run=old.dry_run)
    if job is None:
        ctx["message"] = error
        return templates.TemplateResponse(request, "fragments/error_toast.html", ctx)
    ctx["job"] = job
    return templates.TemplateResponse(request, "fragments/queued.html", ctx)


#: A finished full-library run writes ~400 KB of log. Show the end of it — the summary,
#: the errors, the last files touched — and offer the raw file for the rest.
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
    """One multiplexed stream carrying rendered HTML fragments, not JSON.

    HTMX swaps the payloads directly, so the server stays the only place that knows
    what a job card looks like.
    """
    app = state(request)
    queue = app.jobs.subscribe()

    async def publisher():
        try:
            # Paint the current state immediately: a reconnect must not wait for the
            # next progress tick to show a running job.
            yield {
                "event": "dock",
                "data": render("dock.html", dock_context(app)),
                # Browsers reconnect after a Wi-Fi drop; 3s is the design's promise.
                "retry": 3000,
            }
            while True:
                try:
                    event: JobEvent = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    # Nothing happened. Keep waiting -- EventSourceResponse sends its
                    # own keep-alive comments, so there is nothing for us to emit.
                    continue
                payload = _render_event(app, event)
                if payload is not None:
                    yield payload
        finally:
            app.jobs.unsubscribe(queue)

    # Do NOT poll request.is_disconnected() in the generator. EventSourceResponse is
    # already listening on the same receive channel to detect client disconnects; a
    # second consumer steals those messages, so the stream sees a phantom disconnect,
    # closes, and the browser reconnects. Repeat that a few times and the six
    # connections HTTP/1.1 allows per host are all held by dying streams, at which point
    # every ordinary page load queues behind them and the whole UI appears to hang.
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
            "data": render(
                "term_line.html", {"css": event.css, "text": event.text}
            ),
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
