"""Jinja environment and the formatting filters the design specifies.

The design's typography rule is "monospace for anything the kernel produced", which in
practice means these filters produce the exact strings the blueprint shows: `8.4 MB`,
`21.4G`, `4,812`, `2h ago`, `0:14:52`.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]
_SHORT_UNITS = ["B", "K", "M", "G", "T", "P"]


def _scale(num: float, units: list[str]) -> tuple[float, str]:
    idx = 0
    val = float(num)
    while val >= 1024 and idx < len(units) - 1:
        val /= 1024
        idx += 1
    return val, units[idx]


def hsize(num: int | float | None) -> str:
    """`8.4 MB` — the file-table SIZE column."""
    if num is None:
        return "—"
    val, unit = _scale(num, _UNITS)
    if unit == "B":
        return f"{int(val)} B"
    return f"{val:.1f} {unit}"


def hsize_short(num: int | float | None) -> str:
    """`21.4G` — the compact form used in the rail, breadcrumb and storage column."""
    if num is None:
        return "—"
    val, unit = _scale(num, _SHORT_UNITS)
    if unit == "B":
        return f"{int(val)}B"
    if val >= 100:
        return f"{val:.0f}{unit}"
    return f"{val:.1f}{unit}"


def commafy(num: int | float | None) -> str:
    """`4,812` — counts everywhere."""
    if num is None:
        return "—"
    return f"{int(num):,}"


def reltime(ts: float | None, now: float | None = None) -> str:
    """`14m ago` / `yesterday` / `4d ago` — the LAST SYNC and index-freshness columns."""
    if not ts:
        return "never"
    now = now if now is not None else time.time()
    delta = now - ts
    if delta < 0:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 172800:
        return "yesterday"
    if delta < 2592000:
        return f"{int(delta // 86400)}d ago"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def until(ts: float | None, now: float | None = None) -> str:
    """`in 4m` / `due now` — `reltime` pointed the other way, for the probe's next check.

    Separate from `reltime` rather than folded into its `delta < 0` arm, which answers
    "just now": a timestamp in the past means a clock skew there and a probe that has come
    due here, and the row wants to say so.
    """
    if not ts:
        return "unscheduled"
    now = now if now is not None else time.time()
    delta = ts - now
    if delta <= 0:
        return "due now"
    if delta < 60:
        return f"in {int(delta)}s"
    if delta < 3600:
        return f"in {int(delta // 60)}m"
    if delta < 86400:
        return f"in {int(delta // 3600)}h"
    return f"in {int(delta // 86400)}d"


def freshness(ts: float | None, now: float | None = None) -> str:
    """`fresh 3m` — the rail's index-age readout."""
    if not ts:
        return "never"
    now = now if now is not None else time.time()
    delta = max(0.0, now - ts)
    if delta < 60:
        return f"fresh {int(delta)}s"
    if delta < 3600:
        return f"fresh {int(delta // 60)}m"
    if delta < 86400:
        return f"fresh {int(delta // 3600)}h"
    return f"fresh {int(delta // 86400)}d"


def hhmmss(seconds: float | None) -> str:
    """`0:14:52` — job durations and rsync elapsed times."""
    if seconds is None:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def clock(ts: float | None) -> str:
    """`14:02:11` — the STARTED column."""
    if not ts:
        return "—"
    return time.strftime("%H:%M:%S", time.localtime(ts))


def isodate(ts: float | None) -> str:
    """`2026-08-11` — the MODIFIED column."""
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def asset(name: str) -> str:
    """`/static/app.js?v=1a2b3c` — the stamp is the file's mtime, so a deploy changes the
    URL and the browser has no cached copy to serve.

    Without it a deployed CSS or JS change is simply invisible for a while. StaticFiles
    sends an ETag and a Last-Modified but no Cache-Control, so the browser falls back to
    heuristic freshness — around 10% of the file's age — and serves its copy *without
    revalidating*. Measured: an app.js last modified 23:36 the previous day was still
    being used at 12:34, over an hour after the deploy that replaced it, because 10% of
    its twelve-hour age is 74 minutes. The page HTML is dynamic and never cached, so the
    symptom is markup from the new version driven by script from the old one — which
    reads exactly like a fix that did not work.

    Stat per call rather than once at import: it costs microseconds against five small
    files, and it means `--reload` picks up an edited stylesheet without a restart.
    """
    try:
        stamp = int((STATIC_DIR / name).stat().st_mtime)
    except OSError:
        # A missing file is the mount's problem to report, not this helper's.
        return f"/static/{name}"
    return f"/static/{name}?v={stamp:x}"


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = templates.env
    env.trim_blocks = True
    env.lstrip_blocks = True
    env.filters.update(
        hsize=hsize,
        hsize_short=hsize_short,
        commafy=commafy,
        reltime=reltime,
        until=until,
        freshness=freshness,
        hhmmss=hhmmss,
        clock=clock,
        isodate=isodate,
    )
    # A global, not a filter: it is a URL builder, not a formatter, and base.html calls it
    # as asset('app.js').
    env.globals["asset"] = asset
    return templates


templates = build_templates()


__all__ = [
    "templates",
    "build_templates",
    "asset",
    "hsize",
    "hsize_short",
    "commafy",
    "reltime",
    "freshness",
    "hhmmss",
    "clock",
    "isodate",
]
