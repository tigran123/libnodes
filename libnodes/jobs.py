"""Transfer jobs: the queue, the rsync process, and the progress stream.

Nothing blocks: a push returns a job at once and its progress reaches the browser over SSE.
History lives in SQLite; the live bits (current file, rate, the terminal ring) stay in
memory, and the whole transcript goes to `var/logs/<id>.log`. The rsync flags are the
program's, not the config's -- each one's reason is beside BASE_FLAGS or in `build_argv`.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import inspect
import json
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Sequence

from .config import PULL_EXCLUDES, SKIP_TOPLEVEL, Settings
from .probe import DeviceProbe, rsync_e, ssh_base
from .procs import reap
from .library import LibraryIndex, blob_from_link, held_back, within
from .manifests import Manifests
from .models import Device, DevicesFile

log = logging.getLogger(__name__)

JobState = Literal["queued", "running", "done", "failed", "aborted", "deferred"]

#: Which direction a job moves bytes. On the job rather than read off the device, because
#: history has to keep saying what a finished job was.
JobKind = Literal["push", "pull"]

TERMINAL_STATES = frozenset({"done", "failed", "aborted"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id   TEXT NOT NULL,
  sources     TEXT NOT NULL,
  label       TEXT NOT NULL,
  dest        TEXT,
  state       TEXT NOT NULL,
  created_at  REAL,
  started_at  REAL,
  finished_at REAL,
  -- files_sent counts completed transfers; entries_* count what rsync walked past,
  -- directories included. Keeping them apart is the whole point: see _apply_progress.
  files_sent    INTEGER DEFAULT 0,
  files_total   INTEGER DEFAULT 0,
  -- What the transfer removed at the far end (a mirror) or at this one (a pull). Files
  -- only, never directories, for the reason in _stream.
  files_deleted INTEGER DEFAULT 0,
  entries_done  INTEGER DEFAULT 0,
  entries_total INTEGER DEFAULT 0,
  bytes_done  INTEGER DEFAULT 0,
  bytes_total INTEGER DEFAULT 0,
  bytes_wire  INTEGER DEFAULT 0,
  pct         REAL DEFAULT 0,
  exit_code   INTEGER,
  error       TEXT,
  argv        TEXT,
  attempt     INTEGER DEFAULT 0,
  dry_run     INTEGER DEFAULT 0,
  hold        INTEGER DEFAULT 0,
  adopt       INTEGER DEFAULT 0,
  -- Was this push the whole library rather than a selection? Persisted because it is
  -- half of what decides --delete (the device's `prune` is the other half), and `retry`
  -- re-derives from a stored row long after the route that set it ran.
  full_library INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_jobs_state ON jobs(state);
"""

# Two byte formats: plain `1,234,567` and -h's `734.38K`. We never pass -h, but a parser
# that took only digits once reported 0 bytes for every real transfer
# (tests/data_rsync_human.log), so both are read.
PROGRESS_RE = re.compile(
    r"^\s*([\d,]+(?:\.\d+)?[KMGTP]?)\s+(\d+)%\s+(\S+)\s+(\d+:\d\d:\d\d)"
    r"(?:\s+\(xfr#(\d+),\s+(?:ir-chk|to-chk)=(\d+)/(\d+)\))?"
)

# rsync's closing tally (from stats1), and the only figure that is bytes on the wire rather
# than bytes of file: one push handled 4,379,115,438 bytes of files and sent 6.7 MB.
SUMMARY_RE = re.compile(r"^sent ([\d,]+) bytes\s+received ([\d,]+) bytes")

# Exit 23 is "some files/attrs were not transferred". Every diagnostic starts `rsync:`, so
# when all of them are attribute failures no file's data was missed. That is the standing
# outcome on Android's emulated storage, a FUSE shim with no utimensat: job #1 on nexus10
# delivered both files byte-exact and still exited 23. The `[generator]` tag is optional
# because the device's rsync may predate 3.2. See Device.stores_times.
_RSYNC_PROBLEM_RE = re.compile(r"^rsync: ", re.MULTILINE)
_ATTR_PROBLEM_RE = re.compile(
    r"^rsync: (?:\[[^\]]+\] )?failed to set \w+", re.MULTILINE
)

_MULT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
_SIZE_TOKEN_RE = re.compile(r"^([\d,]+(?:\.\d+)?)([KMGTP]?)$")

# Each file announced in a shape we chose -- `@<length>|<name>`, a directory with a trailing
# slash -- so a filename with a % in it cannot pass for progress. No %i: on a FAT target
# every file differs in permissions for ever, and a no-op run logged 24,616 lines of it.
OUT_FORMAT = "@%l|%n"
FILE_RE = re.compile(r"^@(?P<size>\d*)\|(?P<name>.*)$")

# A deletion has no out-format, so it is matched on rsync's own wording. It reaches us at all
# because naming an out-format raises INFO_DEL (verified against sigmaai.au with exactly
# these flags). The manpage's `*deleting` is the -i form, which we never pass.
DELETE_RE = re.compile(r"^deleting (?P<name>.+)$")

# Only what we parse: one aggregate progress line and the closing stats, no chatter.
INFO_FLAGS = "progress2,flist0,misc0,stats1"

#: A push's flags, which are the program's: the parser reads --info/--out-format, and a
#: config that could drop one would break the app in ways that look like bugs.
#:
#: -L is mandatory for a reader: the library is symlinks into a CAS vault, and without it a
#: device receives dangling links while rsync reports success. A mirror drops it, which is
#: safe only because it also sends the whole root, vault included (`build_argv`).
#: -R keeps a source's path on the device. -O stops rsync stamping and reporting every
#: directory: 3,839 of them around 4 real files in one dry run. No -h: we format numbers.
BASE_FLAGS = ["-a", "-O", "--partial", "-L", "-R"]

#: A pull's flags. Three of BASE_FLAGS are wrong in this direction, not merely unneeded:
#:
#: -L: the books *are* symlinks, and dereferencing would replace them with a second copy of
#: the vault. -a's -l recreates them as links.
#: -R: with a *remote* source it makes the remote's path part of the destination. Measured
#: against sigmaai.au, `rsync -aR … host:/Books/ /Books/` wanted all 20,793 blobs under
#: /Books/Books/, every link there dangling -- no error, no warning.
#: --partial becomes --partial-dir: plain --partial renames an interrupted file to its final
#: name, which in the vault is a blob that does not hash to its own name.
#:
#: -o/-g stay: the receiver is this host as an ordinary user, so rsync never chowns.
#: --delete is added by `build_pull_argv`, beside the cap that bounds it.
PULL_FLAGS = ["-a", "-O", "--partial-dir=.rsync-partial"]

#: How often a progress line is kept in the *log* (the dock still gets every one). progress2
#: prints a line per file-list update whether anything moved or not: job #19 logged 3,855 of
#: them against 16 lines that said anything. 30 s keeps a five-hour pull to ~600, and the
#: last line of every stream is kept regardless.
LOG_PROGRESS_INTERVAL = 30.0

#: How often a job's file-name lines reach the browser, as one batch. The dock shows the
#: last 200 lines of a terminal; four refreshes a second is more than an eye can follow.
LINE_BATCH = 0.25


def _log_note(log, text: str) -> None:
    """One of *our* lines in the job log -- a command or a phase marker -- timestamped.

    Only ours: rsync's own lines must stay as written, because `_ATTR_PROBLEM_RE` anchors on
    `^rsync:`, and a prefix would turn every attrs-only exit 23 back into a failure.
    """
    log.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")

#: Delivered names one job keeps, so an interrupted push can still credit what landed. The
#: library is ~24.6k files.
SENT_CAP = 50_000

#: What `_stream` returns when a command cannot be spawned at all -- the shell's own "command
#: not found". Never retried: three more attempts would fail to find the same binary.
SPAWN_FAILED = 127


def parse_size_token(token: str) -> int:
    """`734.38K` or `1,234,567` -> bytes."""
    match = _SIZE_TOKEN_RE.match(token.strip())
    if not match:
        return 0
    return int(float(match.group(1).replace(",", "")) * _MULT[match.group(2).upper()])


@dataclass
class Job:
    id: int
    device_id: str
    sources: list[str]
    label: str
    dest: str = ""
    state: JobState = "queued"
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    #: Completed transfers, the highest `xfr#N` seen -- not the @-lines, which rsync prints
    #: when a file *starts*.
    files_sent: int = 0
    #: Files selected, from the index at submit and never overwritten, so it means the same
    #: in every view.
    files_total: int = 0
    #: Files this job pruned, at either end, from rsync's `deleting` lines. Directories are
    #: not files.
    files_deleted: int = 0
    #: File-list entries walked (`to-chk`), directories included -- 244 where files_total is
    #: 234 -- so never shown as files.
    entries_done: int = 0
    entries_total: int = 0
    #: Size of the files handled: progress2's counter, the running sum of the @-line sizes.
    #: Not network traffic -- see bytes_wire.
    bytes_done: int = 0
    bytes_total: int = 0
    #: What crossed the link, from the closing summary, so only once finished. Delta
    #: matching makes it far smaller than bytes_done: 6.7 MB against 4.4 GB on one push.
    bytes_wire: int = 0
    pct: float = 0.0
    exit_code: int | None = None
    error: str | None = None
    argv: list[str] = field(default_factory=list)
    attempt: int = 0
    dry_run: bool = False
    #: Deferred, and waiting for an explicit Start however reachable the node becomes.
    hold: bool = False
    #: An Adopt: repair metadata on files the device already has, moving no data.
    adopt: bool = False
    #: A mirror Replicate, which owes the replica its catalog once the files have landed.
    catalog: bool = False
    #: The whole library rather than a selection: half of what lets `Device.prune` add
    #: --delete. Persisted so `retry` re-derives the same transfer.
    full_library: bool = False
    #: "push", or "pull" -- six phases, writing into library_root. Persisted for history
    #: and for `retry`.
    kind: JobKind = "push"

    # Live-only, never persisted.
    #: The files landed but the catalog beside them was not refreshed; the dock draws amber.
    catalog_warning: str = ""
    #: The phase running, for the dock. Separate from `pct`, which means the transfer only.
    phase: str = ""
    current_file: str = ""
    rate: str = ""
    eta: str = ""

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def finished(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def duration(self) -> float | None:
        if self.started_at is None:
            return None
        return (self.finished_at or time.time()) - self.started_at

    @property
    def command(self) -> str:
        return shlex.join(self.argv)


@dataclass
class JobEvent:
    """A change worth pushing to the browser. Rendered to HTML by the route layer."""

    kind: Literal["progress", "line", "done", "dock"]
    job_id: int | None = None
    #: For `line`: the terminal lines since the last one, as `(css, text)`.
    lines: list[tuple[str, str]] = field(default_factory=list)


class JobStore:
    """Durable job history. The design shows past jobs, so this survives a restart."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            # Additive migrations for older databases; SQLite has no ADD COLUMN IF NOT
            # EXISTS. The retired `files_done` is left alone: it held entry counts, and
            # relabelling them as transfers is the confusion files_sent fixed.
            existing = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            for column, ddl in (("hold", "INTEGER DEFAULT 0"),
                                ("adopt", "INTEGER DEFAULT 0"),
                                ("kind", "TEXT DEFAULT 'push'"),
                                ("catalog", "INTEGER DEFAULT 0"),
                                ("files_sent", "INTEGER DEFAULT 0"),
                                ("entries_done", "INTEGER DEFAULT 0"),
                                ("entries_total", "INTEGER DEFAULT 0"),
                                ("files_deleted", "INTEGER DEFAULT 0"),
                                ("bytes_wire", "INTEGER DEFAULT 0"),
                                ("full_library", "INTEGER DEFAULT 0")):
                if column not in existing:
                    with conn:
                        conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl}")
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def create(self, job: Job) -> Job:
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO jobs (device_id, sources, label, dest, state, "
                    "created_at, files_total, bytes_total, argv, attempt, dry_run, "
                    "hold, adopt, kind, catalog, full_library) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        job.device_id,
                        json.dumps(job.sources),
                        job.label,
                        job.dest,
                        job.state,
                        job.created_at,
                        job.files_total,
                        job.bytes_total,
                        json.dumps(job.argv),
                        job.attempt,
                        int(job.dry_run),
                        int(job.hold),
                        int(job.adopt),
                        job.kind,
                        int(job.catalog),
                        int(job.full_library),
                    ),
                )
                job.id = int(cur.lastrowid)
        finally:
            conn.close()
        return job

    def save(self, job: Job) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE jobs SET state=?, started_at=?, finished_at=?, "
                    "files_sent=?, files_deleted=?, files_total=?, entries_done=?, "
                    "entries_total=?, "
                    "bytes_done=?, bytes_total=?, bytes_wire=?, pct=?, "
                    "exit_code=?, error=?, argv=?, attempt=?, dest=?, hold=? "
                    "WHERE id=?",
                    (
                        job.state,
                        job.started_at,
                        job.finished_at,
                        job.files_sent,
                        job.files_deleted,
                        job.files_total,
                        job.entries_done,
                        job.entries_total,
                        job.bytes_done,
                        job.bytes_total,
                        job.bytes_wire,
                        job.pct,
                        job.exit_code,
                        job.error,
                        json.dumps(job.argv),
                        job.attempt,
                        job.dest,
                        int(job.hold),
                        job.id,
                    ),
                )
        finally:
            conn.close()

    def get(self, job_id: int) -> Job | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        finally:
            conn.close()
        return _job_from_row(row) if row else None

    def recent(self, limit: int = 60) -> list[Job]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY "
                "  CASE state WHEN 'running' THEN 0 WHEN 'queued' THEN 1 "
                "             WHEN 'deferred' THEN 2 ELSE 3 END, "
                "  created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [_job_from_row(r) for r in rows]

    def delete(self, job_id: int) -> bool:
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
                return bool(cur.rowcount)
        finally:
            conn.close()

    def clear_finished(self) -> int:
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "DELETE FROM jobs WHERE state IN ('done','failed','aborted')"
                )
                return cur.rowcount or 0
        finally:
            conn.close()

    def unfinished(self) -> list[Job]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state IN ('running','queued','deferred')"
            ).fetchall()
        finally:
            conn.close()
        return [_job_from_row(r) for r in rows]


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        device_id=row["device_id"],
        sources=json.loads(row["sources"]),
        label=row["label"],
        dest=row["dest"] or "",
        state=row["state"],
        created_at=row["created_at"] or 0.0,
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        files_sent=row["files_sent"] or 0,
        files_deleted=row["files_deleted"] or 0,
        files_total=row["files_total"] or 0,
        entries_done=row["entries_done"] or 0,
        entries_total=row["entries_total"] or 0,
        bytes_done=row["bytes_done"] or 0,
        bytes_total=row["bytes_total"] or 0,
        bytes_wire=row["bytes_wire"] or 0,
        pct=row["pct"] or 0.0,
        exit_code=row["exit_code"],
        error=row["error"],
        argv=json.loads(row["argv"] or "[]"),
        attempt=row["attempt"] or 0,
        dry_run=bool(row["dry_run"]),
        # The columns always exist: JobStore.__init__ adds any an older database lacks.
        hold=bool(row["hold"]),
        adopt=bool(row["adopt"]),
        kind=row["kind"] or "push",
        catalog=bool(row["catalog"]),
        full_library=bool(row["full_library"]),
    )


def _connect_timeout(device: Device, defaults) -> int:
    return min(device.timeout_with(defaults), 30)


def _ssh_transport(device: Device, defaults) -> list[str]:
    """The `-e <ssh command>` pair, shared by every rsync this module composes.

    The same ssh the probe uses (`probe.ssh_base`), keepalives included: both ride the one
    multiplexed master the Pi's ssh config opens per device, so a transfer that disagreed
    with the probe would either inherit its settings anyway or wedge behind a dead master.
    Pinned by tests/test_ssh_keepalive.py.
    """
    return ["-e", rsync_e(device, _connect_timeout(device, defaults))]


def build_argv(
    device: Device,
    config: DevicesFile,
    sources: Sequence[str],
    settings: Settings,
    dry_run: bool = False,
    adopt: bool = False,
    whole_library: bool = False,
) -> list[str]:
    """Compose a push as an argv list, never a shell string.

    Run with `cwd=library_root` and `-R`, so `Science/Philology/` lands at
    `<target>/Science/Philology/` and several sources share one invocation. The mode is
    read off the device, so every caller -- the Actions previews included -- gets it.

    `whole_library` is the one thing a caller must say: "these sources are the entire
    library", which a reader's `prune` needs before --delete. Its default is the side that
    deletes nothing.
    """
    defaults = config.defaults

    # First, because no caller can opt out of it: `JobRunner.submit` composes every writing
    # path, `retry` and restart re-adoption included, so a route never taught about
    # upstream still cannot aim a transfer at the library's source.
    if device.is_upstream:
        raise ValueError(
            f"{device.id}: sync_mode upstream is a pull source — "
            "it is never a transfer destination"
        )

    mirror = device.is_mirror

    if mirror:
        # With --delete below, a wrong source list or a root target is data loss.
        if not sources:
            raise ValueError(
                f"{device.id}: refusing a mirror push with no sources — "
                "--delete would empty the target"
            )
        if not device.target.strip("/"):
            raise ValueError(
                f"{device.id}: refusing to mirror onto {device.target!r} — "
                "--delete needs a target below the root"
            )

    argv = [
        "rsync",
        # The only place -L is ever absent: safe because a mirror's source is the whole
        # root, vault included.
        *(f for f in BASE_FLAGS if not (mirror and f == "-L")),
        f"--info={INFO_FLAGS}",
        f"--out-format={OUT_FORMAT}",
    ]
    if dry_run:
        argv.append("-n")

    # The live catalog never rides in the bulk pass; `_replicate_catalog` sends a
    # consistent snapshot afterwards.
    if mirror:
        argv += _catalog_excludes(settings, REPLICATE_SUFFIX)

    # A replica keeps nothing the origin dropped. Kept under -n too: the dry run is the only
    # preview of a prune. Never on an Adopt, whose promise is "change nothing".
    if mirror and not adopt:
        argv.append("--delete")

    # The only --delete aimed at a reader, and four facts must hold at once: the node asked
    # (`prune`), it takes the whole library (`full_sync`, re-checked for `retry`), *these*
    # sources are the library rather than a subtree (a Push of `Science/` must never prune
    # Science/), and it is not an Adopt.
    #
    # The sources are the named top-level categories, so rsync never scans the destination
    # root: a name the library never had (`Websites/` on s4l) survives, and only divergence
    # inside the library's shape is pruned. Inside it, `excludes` survive -- measured against
    # s4l on 2026-09-20: 20 `deleting` lines, 19 of them KOReader's `.sdr` sidecars; with
    # `*.sdr/` excluded, 1. No --max-delete, unlike a pull: what is at risk is a replica of
    # a library this host still holds in full, and the Dry run is the preview.
    prune = (
        whole_library and device.prune and device.full_sync and not mirror and not adopt
    )
    if prune:
        # A mirror's two refusals, for its reason. An empty list is what
        # `full_sync_sources` returns when it cannot read the library root.
        if not sources:
            raise ValueError(
                f"{device.id}: refusing a pruning full sync with no sources — "
                "--delete would empty the target"
            )
        if not device.target.strip("/"):
            raise ValueError(
                f"{device.id}: refusing to prune {device.target!r} — "
                "--delete needs a target below the root"
            )
        argv.append("--delete")

    # The target filesystem decides, not the device type. All three flags, because a
    # filesystem with no permission bits has no owner either -- FAT takes both from the
    # mount -- and rsync as root otherwise chowns every file, which vfat refuses. A file
    # whose chown failed never gets its mtime stamped, so it is re-sent on every push.
    # Measured against the Kobo on 5 byte-identical books: `--no-perms` alone xfr#5 and exit
    # 23; all three xfr#5 and exit 0 (the repair); again xfr#0, 415 B. Deliberately not
    # forgiven in `is_attrs_only` instead: that would hide the re-send loop, not end it.
    if not device.fs_profile.perms:
        argv += ["--no-perms", "--no-owner", "--no-group"]

    # FAT's seconds come in twos, so a stamped time reads back up to a second early:
    # 8,786 of 24,620 files re-sent on every push to a FAT32 card, 0 with the window.
    # Per filesystem, because on ext4 the exact comparison is the point.
    if device.fs_profile.modify_window:
        argv.append(f"--modify-window={device.fs_profile.modify_window}")

    if adopt:
        # The files are there with the wrong mtimes; --size-only skips them while -a still
        # repairs the times. Measured: 51.6 MB / 14 files reconciled in 0.66 s, 0 bytes
        # moved, and an ordinary sync itemises nothing afterwards.
        argv.append("--size-only")

    # A target that cannot store an mtime (see Device.stores_times) needs both flags.
    # Measured on nexus10 against byte-identical files, `-n -i`:
    #
    #   -a                 <f..t......   re-sends every push   exit 23
    #   -a --no-times      <f..T......   re-sends every push   exit 0
    #   -a --size-only     .f..t......   sends nothing         exit 23
    #   -a --size-only --no-times        sends nothing         exit 0
    #
    # The one exception to "--size-only is Adopt's": affordable because a changed book gets
    # a new blob, which a scan compares. --modify-window above is inert here and stays.
    if not device.stores_times:
        argv.append("--no-times")
        if not adopt:  # Adopt already added it
            argv.append("--size-only")

    bandwidth = device.bandwidth_with(defaults)
    if bandwidth:
        argv.append(f"--bwlimit={bandwidth}")
    for pattern in device.excludes_with(defaults):
        argv.append(f"--exclude={pattern}")

    argv += _ssh_transport(device, defaults)

    root = Path(settings.library_root)
    if mirror:
        # The root itself: --delete prunes only directories in the transfer, so with the
        # top-level names enumerated a stray top-level file outlives every replicate
        # (measured on a local pair). `./`, not `""`, which would defeat -R.
        argv.append("./")
    else:
        for src in sources:
            rel = src.strip("/")
            abs_src = root / rel if rel else root
            argv.append(f"{rel}/" if abs_src.is_dir() else rel)

    target = device.target.rstrip("/")
    argv.append(f"{device.effective_user}@{device.host}:{target}/")
    return argv


def full_sync_sources(settings: Settings) -> list[str]:
    """Every top-level library directory, minus the infrastructure ones."""
    root = Path(settings.library_root)
    try:
        names = sorted(
            e.name
            for e in os.scandir(root)
            if e.name not in SKIP_TOPLEVEL and e.is_dir(follow_symlinks=False)
        )
    except OSError:
        return []
    return names


def mirror_sources(settings: Settings) -> list[str]:
    """Every top-level entry, sorted, with nothing held back: the mirror's whole root.

    No SKIP_TOPLEVEL -- the vault is mandatory when the symlinks are kept, `Recommended/`
    costs a few hundred bytes as links, and urantia-library/ is the mode's declared cost --
    and files as well as directories.

    These are the job's *logical* sources, what `_estimate` prices and `_update_manifest`
    records; rsync itself is handed `./` (see `build_argv`). An empty list still means
    "refuse": it says we could not read the library.
    """
    root = Path(settings.library_root)
    try:
        return sorted(e.name for e in os.scandir(root))
    except OSError:
        return []


def _ssh_command(device: Device, defaults, remote: str) -> list[str]:
    """`ssh … user@host <remote>`, with `remote` **one** already-quoted word.

    ssh does not pass argv through: it joins everything after `user@host` with spaces for a
    shell on the far side, so a list arrives unquoted and re-split. Job #18's snapshot script
    came back as `SyntaxError` and `bash: syntax error near unexpected token` -- while its log
    showed the command correctly quoted, because the log re-quotes the argv.
    """
    return [
        *ssh_base(device, _connect_timeout(device, defaults)),
        f"{device.effective_user}@{device.host}",
        remote,
    ]


def catalog_rel(settings: Settings) -> str | None:
    """The catalog's path inside the library, or None if `catalog_db` lies outside it.

    A CAS node has our tree's shape, so the remote copy is `<target>/<this>` with nothing
    to configure. None makes the catalog phase report itself unavailable.
    """
    try:
        return str(Path(settings.catalog_db).relative_to(Path(settings.library_root)))
    except ValueError:
        return None


def _require_catalog_rel(settings: Settings) -> str:
    """`catalog_rel`, for a builder that has nothing to build without it."""
    rel = catalog_rel(settings)
    if rel is None:
        raise ValueError(
            f"catalog_db {settings.catalog_db} is not inside library_root "
            f"{settings.library_root} — there is no remote path to derive"
        )
    return rel


def _catalog_excludes(settings: Settings, snapshot_suffix: str) -> list[str]:
    """Keep the live catalog out of a bulk pass: the database, its WAL pair and the named
    snapshot, one by one. rsync reads a WAL database's three files at three instants, so a
    checkpoint between them hands over a torn catalog; it travels in its own phase instead.
    By name rather than `/.data/db/`, so anything else living there still moves."""
    rel = catalog_rel(settings)
    if not rel:
        return []
    return [f"--exclude=/{rel}{side}" for side in ("", *CATALOG_SIDECARS, snapshot_suffix)]


def _one_file_rsync(device: Device, config: DevicesFile, dry_run: bool = False) -> list[str]:
    """The head of an rsync that moves one named file. No -R (see PULL_FLAGS) and
    --partial-dir, so an interrupted catalog never lands under the real name."""
    argv = [
        "rsync",
        "-a",
        "--partial-dir=.rsync-partial",
        f"--info={INFO_FLAGS}",
        f"--out-format={OUT_FORMAT}",
    ]
    if dry_run:
        argv.append("-n")
    return argv + _ssh_transport(device, config.defaults)


#: A WAL database's sidecars. Never transferred -- they belong to whichever process last
#: had the file open -- and removed before a new catalog lands.
CATALOG_SIDECARS = ("-wal", "-shm")

#: Where the upstream snapshots its own catalog: beside it, not in the far end's staging.
SNAPSHOT_SUFFIX = ".pull-snapshot"

#: The upstream's snapshot, taken with its webapp still serving: `Connection.backup` reads a
#: WAL database without blocking its writer (82 tables, 7,224 pages, 1.07 s on sigmaai.au).
#: python3 because that host has no sqlite3 binary. Opened read-write because a read-only
#: WAL connection still maps the -shm; it writes nothing. One line, so it survives the
#: remote shell as one quoted word and reads back in the log.
_SNAPSHOT_PY = (
    "import sqlite3,sys,os; "
    "s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); "
    "s.backup(d); d.close(); s.close(); "
    # Double quotes inside, because shlex wraps the script in single ones.
    'print("# snapshot %d bytes" % os.path.getsize(sys.argv[2]))'
)


def build_pull_argv(
    device: Device,
    config: DevicesFile,
    settings: Settings,
    dry_run: bool = False,
) -> list[str]:
    """Compose a pull's long pass: the remote's whole library, into ours.

    A separate function, not `build_argv(direction=…)`: that one's branches are facts about
    the device *as a destination* (perms, FAT's window, --no-times), all wrong here, and a
    `direction` parameter's default would be the dangerous direction. The fixture's upstream
    declares `fs: vfat` and `stores_times: false` so their absence is proved by construction.

    `--delete` is the only flag that removes files from *this* host: an upstream is the
    source of truth, and without it /Books only grew (three stale objects after four months,
    measured 2026-09-19). Bounded by the excludes, which rsync never deletes; by
    `--max-delete` (see `Settings.pull_max_delete`), for an upstream that is half mounted;
    and by the dry run, under which the cap applies too.
    """
    if not device.is_upstream:
        # The reverse of build_argv's refusal: neither can be talked into the other's direction.
        raise ValueError(
            f"{device.id}: only a sync_mode upstream node is pulled from"
        )

    defaults = config.defaults
    argv = [
        "rsync",
        *PULL_FLAGS,
        f"--info={INFO_FLAGS}",
        f"--out-format={OUT_FORMAT}",
    ]
    if dry_run:
        argv.append("-n")

    for pattern in device.pull_excludes_with(PULL_EXCLUDES):
        argv.append(f"--exclude={pattern}")

    argv += _catalog_excludes(settings, SNAPSHOT_SUFFIX)

    # After the excludes, which are what keeps this from being a whole-library prune.
    argv.append("--delete")
    if settings.pull_max_delete >= 0:
        argv.append(f"--max-delete={settings.pull_max_delete}")

    bandwidth = device.bandwidth_with(defaults)
    if bandwidth:
        argv.append(f"--bwlimit={bandwidth}")

    argv += _ssh_transport(device, defaults)

    target = device.target.rstrip("/")
    argv.append(f"{device.effective_user}@{device.host}:{target}/")
    # Absolute, not `.`. The argv is printed in the dock, in the Actions dialog and at the
    # head of the log, and "where did 250 GB just land" should not need the reader to know
    # what cwd the runner used.
    argv.append(f"{str(settings.library_root).rstrip('/')}/")
    return argv


def build_catalog_argv(
    device: Device,
    config: DevicesFile,
    settings: Settings,
    dry_run: bool = False,
) -> list[str]:
    """One file: the remote's snapshot, landing as our `lib.db`.

    Named on both sides, which is what makes it a rename as well as a copy. No -R for the
    reason in PULL_FLAGS — with it this would arrive at `<library_root>/<target>/…`
    instead. rsync writes a temp file in the destination directory and renames it into
    place, so the swap itself is atomic; the service stop exists so nothing holds the old
    file open and so the stale -wal beside it can go first.
    """
    rel = _require_catalog_rel(settings)
    argv = _one_file_rsync(device, config, dry_run=dry_run)
    target = device.target.rstrip("/")
    argv.append(f"{device.effective_user}@{device.host}:{target}/{rel}{SNAPSHOT_SUFFIX}")
    argv.append(str(settings.catalog_db))
    return argv


def snapshot_argv(device: Device, config: DevicesFile, settings: Settings) -> list[str]:
    """Ask the upstream to snapshot its own live catalog, without stopping it."""
    rel = _require_catalog_rel(settings)
    target = device.target.rstrip("/")
    return _ssh_command(
        device,
        config.defaults,
        shlex.join(
            [
                "python3",
                "-c",
                _SNAPSHOT_PY,
                f"{target}/{rel}",
                f"{target}/{rel}{SNAPSHOT_SUFFIX}",
            ]
        ),
    )


def cleanup_argv(device: Device, config: DevicesFile, settings: Settings) -> list[str]:
    """Remove the snapshot from the upstream. Best-effort, and never fatal.

    Run on the failure path too: an abandoned `.pull-snapshot` is 30 MB of production disk
    and would turn up in the next scan's backlog, which is the list that is supposed to
    mean "books you have not pulled".
    """
    rel = _require_catalog_rel(settings)
    target = device.target.rstrip("/")
    # Quoted for the same reason, even though nothing in this one has a space today: the
    # target comes out of a hand-edited devices.yaml, and "it happens to contain no shell
    # metacharacters" is not a property anything here enforces.
    return _ssh_command(
        device,
        config.defaults,
        shlex.join(["rm", "-f", f"{target}/{rel}{SNAPSHOT_SUFFIX}"]),
    )


#: The snapshot a *mirror* is sent, taken beside our own catalog. Distinct from the
#: pull's suffix so a host that is both an upstream's downstream and a mirror's origin --
#: which pi5 is -- can never confuse one job's temp file for the other's.
REPLICATE_SUFFIX = ".replicate-snapshot"


def snapshot_catalog(settings: Settings, dest: Path) -> int:
    """Copy this host's live catalog to `dest`, consistently. Returns its size.

    `Connection.backup`, as the pull's `_SNAPSHOT_PY` does on the far end, so this host's
    reader keeps serving. In-process because the database is local; the caller runs it off
    the event loop.
    """
    src = sqlite3.connect(str(settings.catalog_db))
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest.stat().st_size


def replicate_catalog_argv(
    device: Device, config: DevicesFile, settings: Settings
) -> list[str]:
    """Send one file: our snapshot, landing on the replica as its `lib.db`."""
    rel = _require_catalog_rel(settings)
    argv = _one_file_rsync(device, config)
    argv.append(f"{settings.catalog_db}{REPLICATE_SUFFIX}")
    argv.append(f"{device.effective_user}@{device.host}:{device.target.rstrip('/')}/{rel}")
    return argv


def remote_reader_argv(
    device: Device, config: DevicesFile, settings: Settings
) -> list[str]:
    """Ask the replica whether anything there is reading the catalog right now.

    `local_service` names the application, so the same unit name is asked about there. A
    replica that never heard of it answers `inactive`, which is right; `|| true` keeps a
    systemd-less target's answer a question rather than a failure.
    """
    unit = settings.local_service
    if not unit:
        # `systemctl is-active ''` asks nothing and answers `inactive`: a false all-clear.
        raise ValueError("no LIBNODES_LOCAL_SERVICE declared — there is no unit to ask about")
    return _ssh_command(
        device,
        config.defaults,
        shlex.join(["systemctl", "is-active", unit]) + " || true",
    )


def remote_sidecar_argv(
    device: Device, config: DevicesFile, settings: Settings
) -> list[str]:
    """Drop the replica's stale -wal/-shm before its new catalog lands: an old WAL applied
    over a new database is how a catalog is lost rather than merely not refreshed."""
    rel = _require_catalog_rel(settings)
    target = device.target.rstrip("/")
    return _ssh_command(
        device,
        config.defaults,
        shlex.join(["rm", "-f", *[f"{target}/{rel}{s}" for s in CATALOG_SIDECARS]]),
    )


def service_argv(verb: str, settings: Settings) -> list[str]:
    """`systemctl <verb> <unit>` for the unit on *this* host.

    No sudo: the unit's NoNewPrivileges=yes makes it inert, so polkit authorises this
    (deploy/50-libnodes-urantia.rules). `--no-ask-password` fails fast with no agent, like
    BatchMode on ssh. The unit keeps its `.service` suffix so it matches the rule exactly.
    """
    return ["systemctl", "--no-ask-password", verb, settings.local_service]


class JobRunner:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        index: LibraryIndex,
        manifests: Manifests,
        probe: DeviceProbe,
        devices,
        on_library_changed=None,
    ) -> None:
        #: Rebuilds the index after a pull, the only job that changes `library_root`; may be
        #: async, in which case the pull waits for it. A callback, because AppState
        #: constructs the runner and not the other way round.
        self._on_library_changed = on_library_changed
        self.settings = settings
        self.store = store
        self.index = index
        self.manifests = manifests
        self.probe = probe
        self.devices = devices

        self._live: dict[int, Job] = {}
        self._terms: dict[int, deque[tuple[str, str]]] = {}
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        #: Names off the @-lines, in rsync's order, so a run that dies part-way can say what
        #: it delivered. `None` once past SENT_CAP: a truncated list is no longer a prefix.
        self._sent: dict[int, list[str] | None] = {}
        #: The same for `deleting` lines, so `_debit` can retract those manifest rows.
        self._deleted: dict[int, list[str] | None] = {}
        #: Terminal lines not yet sent to the browser, and when each job last sent some.
        #: See `_append_line`.
        self._unsent_lines: dict[int, list[tuple[str, str]]] = {}
        self._lines_at: dict[int, float] = {}
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._subs: set[asyncio.Queue] = set()
        self._watcher: asyncio.Task | None = None
        #: Who may run at once -- see `_admit`. Pushes share admission, a pull takes it
        #: alone, and a waiting pull holds new pushes back so a busy fleet cannot starve it.
        self._admission = asyncio.Condition()
        self._pushes = 0
        self._pulling = False
        self._pulls_waiting = 0
        #: One job per device at a time. Two rsyncs into one target race on its temp files
        #: -- a pruning run's --delete removes the other's in-flight `.name.XXXXXX` -- and
        #: two pulls would both rewrite the vault and both take the local service down.
        self._device_locks: dict[str, asyncio.Lock] = {}
        #: Children `abort` must not reach -- `_capture`'s questions and the pull's
        #: `systemctl` calls -- reaped by `stop()` like `_procs`, which holds at most one
        #: abortable transfer per job.
        self._probe_procs: set = set()

    # --- accessors --------------------------------------------------------

    def get(self, job_id: int) -> Job | None:
        return self._live.get(job_id) or self.store.get(job_id)

    def recent(self, limit: int = 60) -> list[Job]:
        jobs = self.store.recent(limit)
        return [self._live.get(j.id, j) for j in jobs]

    def active(self) -> list[Job]:
        """Jobs the dock should show: running, queued or deferred, newest last."""
        live = [j for j in self._live.values() if not j.finished]
        live.sort(key=lambda j: j.created_at)
        return live

    def settled(self) -> list[Job]:
        """Finished jobs still pinned in the dock, awaiting Dismiss."""
        done = [j for j in self._live.values() if j.finished]
        done.sort(key=lambda j: j.finished_at or 0)
        return done

    def dismiss_finished(self) -> None:
        for job_id in [j.id for j in self.settled()]:
            self.dismiss(job_id)

    def terminal(self, job_id: int) -> list[tuple[str, str]]:
        return list(self._terms.get(job_id, ()))

    def counts(self) -> tuple[int, int]:
        running = sum(1 for j in self._live.values() if j.state == "running")
        pending = sum(
            1 for j in self._live.values() if j.state in ("queued", "deferred")
        )
        return running, pending

    # --- events -----------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _emit(self, event: JobEvent) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled reader must not stall the transfer, so a progress tick or a
                # terminal line is simply dropped. A `dock` or `done` must not be: SSE mode
                # does not poll, and a lost `done` left the card reading "running" until the
                # page reconnected. The backlog goes instead, replaced by one `dock`, which
                # repaints every card -- finished ones included -- from scratch.
                if event.kind in ("dock", "done"):
                    while not q.empty():
                        q.get_nowait()
                    q.put_nowait(JobEvent("dock"))

    def _append_line(self, job: Job, text: str, css: str = "") -> None:
        ring = self._terms.setdefault(
            job.id, deque(maxlen=self.settings.term_ring)
        )
        ring.append((css, text))
        self._unsent_lines.setdefault(job.id, []).append((css, text))
        # File names arrive by the thousand -- a Replicate or a first pull prints ~45k --
        # and one event each was one render and one SSE frame per open tab apiece. They go
        # in batches. Every other kind of line is rare and flushes at once, so an error,
        # a phase or a summary is never the one left waiting.
        if css or time.monotonic() - self._lines_at.get(job.id, 0.0) >= LINE_BATCH:
            self._flush_lines(job.id)

    def _flush_lines(self, job_id: int) -> None:
        lines = self._unsent_lines.pop(job_id, None)
        if lines:
            self._lines_at[job_id] = time.monotonic()
            self._emit(JobEvent("line", job_id, lines=lines))

    # --- submission -------------------------------------------------------

    def submit(
        self,
        device: Device,
        sources: Sequence[str],
        *,
        label: str | None = None,
        deferred: bool = False,
        dry_run: bool = False,
        hold: bool = False,
        adopt: bool = False,
        whole_library: bool = False,
    ) -> Job:
        config = self.devices.config
        argv = build_argv(
            device,
            config,
            sources,
            self.settings,
            dry_run=dry_run,
            adopt=adopt,
            whole_library=whole_library,
        )
        files_total, bytes_total = self._estimate(
            sources, mirror=device.is_mirror, excludes=device.excludes_with(config.defaults)
        )

        job = Job(
            id=0,
            device_id=device.id,
            sources=list(sources),
            label=label or _label_for(sources),
            dest=f"{device.effective_user}@{device.host}:{device.target.rstrip('/')}/",
            state="deferred" if deferred else "queued",
            created_at=time.time(),
            files_total=files_total,
            bytes_total=bytes_total,
            argv=argv,
            dry_run=dry_run,
            hold=hold and deferred,
            adopt=adopt,
            full_library=whole_library,
            # A Replicate only: an Adopt sends the same root to repair timestamps, and a
            # reader has no catalog.
            catalog=device.is_mirror and not adopt,
        )
        self.store.create(job)
        self._live[job.id] = job
        self._terms[job.id] = deque(maxlen=self.settings.term_ring)
        self._append_line(job, f"$ {job.command}", "cmd")
        if not deferred:
            self._queue.put_nowait(job.id)
        self._emit(JobEvent("dock"))
        return job

    def submit_pull(
        self,
        device: Device,
        *,
        deferred: bool = False,
        dry_run: bool = False,
    ) -> Job:
        """Queue a pull from an upstream node.

        No `_estimate`: the local index cannot price what the far end holds, and the bar
        comes from rsync's `to-chk` anyway. `sources` is the remote root, recorded for the
        Jobs table only; `build_pull_argv` does not read it.
        """
        config = self.devices.config
        argv = build_pull_argv(device, config, self.settings, dry_run=dry_run)
        job = Job(
            id=0,
            device_id=device.id,
            sources=[device.target.rstrip("/") + "/"],
            label="(pull · whole root)",
            # A pull's destination is this host; the dock's arrow reads it.
            dest=f"{str(self.settings.library_root).rstrip('/')}/",
            state="deferred" if deferred else "queued",
            created_at=time.time(),
            argv=argv,
            dry_run=dry_run,
            kind="pull",
        )
        self.store.create(job)
        self._live[job.id] = job
        self._terms[job.id] = deque(maxlen=self.settings.term_ring)
        self._append_line(job, f"$ {job.command}", "cmd")
        if not deferred:
            self._queue.put_nowait(job.id)
        self._emit(JobEvent("dock"))
        return job

    def _estimate(
        self, sources: Sequence[str], *, mirror: bool = False, excludes: Sequence[str] = ()
    ) -> tuple[int, int]:
        """Totals from the index, so the dock has numbers before rsync does, less what
        `excludes` hold back (`excluded_roots`).

        A mirror also sends the vault, which is not indexed: its files are added to the
        count, but not its bytes, because a link's indexed size already is its blob's.
        """
        roots = self.index.excluded_roots(excludes)
        files = 0
        size = 0
        for src in sources:
            entry = self.index.entry(src)
            if entry is None:
                continue
            out_files, out_bytes = held_back(entry, roots)
            files += ((entry.files or 0) if entry.is_dir else 1) - out_files
            size += entry.size - out_bytes
        if mirror:
            vault_files, _vault_bytes = self.index.vault_totals()
            files += vault_files
        return files, size

    async def abort(self, job_id: int) -> Job | None:
        job = self._live.get(job_id)
        if job is None:
            return self.store.get(job_id)
        proc = self._procs.get(job_id)
        if proc is not None and proc.returncode is None:
            proc.terminate()
        elif job.state in ("queued", "deferred"):
            job.state = "aborted"
            job.finished_at = time.time()
            self.store.save(job)
            self._emit(JobEvent("done", job.id))
            self._emit(JobEvent("dock"))
        return job

    def dismiss(self, job_id: int) -> None:
        """Drop a job from the dock, leaving its history row. An unfinished job is aborted
        first: a deferred job whose device never returns must still be removable."""
        job = self._live.get(job_id)
        if job is None:
            return
        if not job.finished:
            job.state = "aborted"
            job.finished_at = time.time()
            job.error = job.error or "dismissed"
            self.store.save(job)
        self._forget(job_id)
        self._emit(JobEvent("dock"))

    def _forget(self, job_id: int) -> None:
        """Drop everything held in memory for one job."""
        for per_job in (self._live, self._terms, self._sent, self._deleted,
                        self._unsent_lines, self._lines_at):
            per_job.pop(job_id, None)

    def start_now(self, job_id: int) -> Job | None:
        """Release a held job (or push a deferred one through) immediately."""
        job = self._live.get(job_id)
        if job is None or job.state not in ("deferred", "queued"):
            return job
        job.hold = False
        job.state = "queued"
        self.store.save(job)
        self._queue.put_nowait(job.id)
        self._emit(JobEvent("dock"))
        return job

    async def cancel(self, job_id: int) -> bool:
        """Stop a job if it is live, then delete it from history entirely.

        This is what the Jobs table's ✕ does. `dismiss` only hides a card; a queued or
        deferred job the user no longer wants has to actually go away.
        """
        job = self._live.get(job_id)
        if job is not None:
            proc = self._procs.get(job_id)
            if proc is not None and proc.returncode is None:
                proc.terminate()
                # Let the runner observe the exit and settle the job's own state.
                for _ in range(40):
                    await asyncio.sleep(0.05)
                    if self._live.get(job_id) is None or self._live[job_id].finished:
                        break
        self._forget(job_id)

        removed = self.store.delete(job_id)
        log = self.settings.logs_dir / f"{job_id}.log"
        try:
            log.unlink(missing_ok=True)
        except OSError:
            pass
        self._emit(JobEvent("dock"))
        return removed

    # --- execution --------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._run(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                job = self._live.get(job_id)
                if job is not None:
                    job.state = "failed"
                    job.error = str(exc)
                    job.finished_at = time.time()
                    self.store.save(job)
                    self._emit(JobEvent("done", job.id))
            finally:
                self._queue.task_done()

    # --- pull -------------------------------------------------------------

    @property
    def _service_hold(self) -> Path:
        """Breadcrumb: "LibNodes stopped the local service and owes it a start".

        The `finally` in `_run_pull` cannot cover this process going away inside the window
        -- a restart, the routine dev loop here -- and `asyncio.shield` does not help once the
        loop closes. So the durable half is a file, read by `start()`.
        """
        return self.settings.state_dir / "service-hold.json"

    async def _service(self, job: Job, verb: str, log) -> int:
        return await self._stream(
            job, service_argv(verb, self.settings), log, track=False, abortable=False
        )

    def _phase(self, job: Job, text: str, log=None) -> None:
        job.phase = text
        self._append_line(job, f"— {text}", "info")
        if log is not None:
            _log_note(log, f"— {text}")
        self._emit(JobEvent("progress", job.id))

    async def _replicate_catalog(self, job: Job, log) -> None:
        """Give a replica a consistent catalog, after its files have landed.

        Never changes the exit code: the books did arrive, and a stale catalog is an amber
        note. No privilege anywhere -- ask whether the replica is reading the file, snapshot
        ours, drop its stale WAL, send the snapshot in as lib.db.
        """
        device = self.devices.config.by_id.get(job.device_id)
        if device is None:
            return
        config = self.devices.config
        rel = catalog_rel(self.settings)
        if rel is None:
            job.catalog_warning = (
                f"catalog not replicated — {self.settings.catalog_db} is not inside "
                f"{self.settings.library_root}, so there is no remote path to derive"
            )
            self._append_line(job, job.catalog_warning, "warn")
            return
        if not Path(self.settings.catalog_db).exists():
            # Nothing to send, and nothing wrong: catalog_db is documented as optional.
            return

        self._phase(job, "2/2 · catalog", log)

        if self.settings.local_service:
            # Refuse rather than overwrite a database being read. No remote stop: taking a
            # machine's services down from another host is more than a replicate may claim.
            reader = await self._capture(remote_reader_argv(device, config, self.settings))
            if reader is not None and reader.strip().startswith("active"):
                job.catalog_warning = (
                    f"catalog not replicated — {self.settings.local_service} is running on "
                    f"{device.name} and the swap would overwrite a database it is reading. "
                    "Stop it there and replicate again."
                )
                self._append_line(job, job.catalog_warning, "warn")
                return

        snapshot = Path(f"{self.settings.catalog_db}{REPLICATE_SUFFIX}")
        try:
            _log_note(log, f"— snapshot {self.settings.catalog_db} -> {snapshot.name}")
            size = await asyncio.to_thread(snapshot_catalog, self.settings, snapshot)
            self._append_line(job, f"catalog snapshot · {size:,} bytes", "prog")

            if await self._stream(
                job, remote_sidecar_argv(device, config, self.settings), log, track=False
            ) != 0:
                job.catalog_warning = (
                    "catalog not replicated — could not clear the stale write-ahead log "
                    f"on {device.name}"
                )
                self._append_line(job, job.catalog_warning, "warn")
                return
            if await self._stream(
                job, replicate_catalog_argv(device, config, self.settings), log,
                track=False,
            ) != 0:
                job.catalog_warning = "catalog not replicated — the transfer failed"
                self._append_line(job, job.catalog_warning, "warn")
        finally:
            snapshot.unlink(missing_ok=True)

    async def _capture(self, argv: list[str]) -> str | None:
        """Run a short command and return its stdout, or None if it could not run. In
        `_probe_procs`, so `stop()` reaps it and `abort()` leaves it alone."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError:
            return None
        self._probe_procs.add(proc)
        try:
            out, _ = await proc.communicate()
        finally:
            # Only a child that has exited, or `stop()` cannot reap it (see procs.py).
            if proc.returncode is not None:
                self._probe_procs.discard(proc)
        return out.decode("utf-8", errors="replace")

    async def _run_pull(self, job: Job, log) -> int:
        """A preflight and six phases, in one coroutine so one `finally` spans them.

        "The local service comes back whatever happens" is a try/finally, which cannot span
        chained jobs. Abort needs no special handling: it terminates the child without
        cancelling this task, so `_stream` returns an ordinary exit code and the `finally`
        runs.
        """
        device = self.devices.config.by_id.get(job.device_id)
        if device is None:
            self._append_line(job, f"unknown device {job.device_id}", "err")
            return 1
        config = self.devices.config
        unit = self.settings.local_service

        if unit and not job.dry_run and catalog_rel(self.settings) is not None:
            # May this process manage the unit at all? A missing polkit rule used to surface
            # at phase 3, after the whole transfer. `start` on a running unit is a no-op on
            # the same authorisation path as `stop`; an inactive unit is left alone.
            self._phase(job, f"preflight · {unit}", log)
            if await self._service(job, "is-active", log) == 0:
                probe = await self._service(job, "start", log)
                if probe != 0:
                    self._append_line(
                        job,
                        f"not authorised to manage {unit} — install "
                        "/etc/polkit-1/rules.d/50-libnodes-urantia.rules (deploy/README.md). "
                        "Nothing was transferred.",
                        "err",
                    )
                    return probe

        self._phase(job, "1/6 · library", log)
        code = await self._stream(job, job.argv, log)
        if code != 0:
            return code
        if job.dry_run:
            # Before the snapshot: a preview writes nothing there and stops nothing here.
            self._append_line(
                job,
                "dry run · nothing written, no snapshot taken, no service stopped",
                "prog",
            )
            return code

        rel = catalog_rel(self.settings)
        if rel is None:
            job.catalog_warning = (
                f"catalog not refreshed — {self.settings.catalog_db} is not inside "
                f"{self.settings.library_root}, so there is no remote path to derive"
            )
            self._append_line(job, job.catalog_warning, "warn")
            return code
        if not self.settings.local_service:
            job.catalog_warning = (
                "catalog not refreshed — no LIBNODES_LOCAL_SERVICE declared, and "
                "overwriting a live WAL database under a running reader corrupts it"
            )
            self._append_line(job, job.catalog_warning, "warn")
            return code

        self._phase(job, "2/6 · snapshot", log)
        snap = await self._stream(
            job, snapshot_argv(device, config, self.settings), log, track=False
        )
        if snap != 0:
            job.catalog_warning = "catalog not refreshed — the upstream snapshot failed"
            self._append_line(job, job.catalog_warning, "warn")
            await self._cleanup(job, device, config, log)
            return code

        stopped = False
        try:
            self._phase(job, f"3/6 · stopping {unit}", log)
            # Asked again: hours may have passed since the preflight.
            if await self._service(job, "is-active", log) != 0:
                # Nothing reads the catalog, so no quiet window -- and no start afterwards of
                # a service somebody stopped.
                self._append_line(
                    job, f"{unit} is not running · swapping without a stop", "prog"
                )
            else:
                self._service_hold.parent.mkdir(parents=True, exist_ok=True)
                self._service_hold.write_text(
                    json.dumps({"unit": unit, "job": job.id, "at": time.time()}),
                    encoding="utf-8",
                )
                if await self._service(job, "stop", log) != 0:
                    # Nothing is down, so nothing to start on the way out.
                    self._service_hold.unlink(missing_ok=True)
                    job.catalog_warning = (
                        f"catalog not refreshed — could not stop {unit}. Install "
                        "/etc/polkit-1/rules.d/50-libnodes-urantia.rules; see "
                        "deploy/README.md"
                    )
                    self._append_line(job, job.catalog_warning, "err")
                    return code
                stopped = True

            self._phase(job, "4/6 · catalog", log)
            # The stale WAL goes *before* the new database lands: an old WAL applied over a
            # new file loses the catalog. A clean stop has usually removed it already.
            for side in CATALOG_SIDECARS:
                Path(str(self.settings.catalog_db) + side).unlink(missing_ok=True)
            cat = await self._stream(
                job, build_catalog_argv(device, config, self.settings), log, track=False
            )
            if cat != 0:
                job.catalog_warning = "catalog not refreshed — the swap failed"
                self._append_line(job, job.catalog_warning, "err")
        finally:
            if stopped:
                self._phase(job, f"5/6 · starting {unit}", log)
                if await self._service(job, "start", log) == 0:
                    self._service_hold.unlink(missing_ok=True)
                else:
                    job.catalog_warning = (
                        f"{unit} DID NOT RESTART — this host's site is down. "
                        "Start it by hand."
                    )
                    self._append_line(job, job.catalog_warning, "err")

        await self._cleanup(job, device, config, log)
        return code

    async def _cleanup(self, job: Job, device: Device, config, log) -> None:
        """Remove the snapshot from the upstream, on failure too and never fatally: an
        abandoned one is 30 MB of somebody else's disk, and would show in the backlog."""
        self._phase(job, "6/6 · cleanup", log)
        try:
            await self._stream(
                job, cleanup_argv(device, config, self.settings), log, track=False
            )
        except Exception as exc:  # noqa: BLE001 - tidying must not fail a landed pull
            self._append_line(job, f"could not remove the remote snapshot: {exc}", "warn")

    async def _stream(
        self, job: Job, argv: list[str], log, *, track: bool = True, abortable: bool = True
    ) -> int:
        """Run one subprocess to completion, feeding its output to the log, dock and ring.

        `track=False` keeps a later phase from *redefining* the job's numbers, which are
        assignments: job #31's library phase moved 15.3 MB across 63,518 entries and was
        recorded as the 29.7 MB catalog swap four phases later. Its output still reaches the
        log and the terminal in full.

        `self._procs[job.id]` holds at most one process at any instant -- `abort`, `cancel`
        and `stop` assume so. `abortable=False` registers it where only `stop()` reaches:
        killing a `systemctl stop` client does not cancel the stop, so an Abort there left
        the service down with the runner believing it had never gone.

        Returns the exit code; a command that cannot be spawned is 127, and the caller
        decides what that means, as for any other code.
        """
        # An ssh argv ends in one word that is a shell command for the far side; print it as
        # the remote shell will see it, not re-quoted (job #18's log hid a quoting bug so).
        if argv and argv[0] == "ssh" and len(argv) > 1:
            _log_note(log, f"$ {shlex.join(argv[:-1])}")
            log.write(f"           remote: {argv[-1]}\n")
        else:
            _log_note(log, f"$ {shlex.join(argv)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.settings.library_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            log.write(f"{exc}\n")
            self._append_line(job, f"cannot run {argv[0]}: {exc}", "err")
            return SPAWN_FAILED

        if abortable:
            self._procs[job.id] = proc
        else:
            self._probe_procs.add(proc)
        last_push = 0.0
        last_term = 0.0
        last_log = 0.0
        #: The latest progress line held back from the log, flushed at the end.
        pending = ""

        assert proc.stdout is not None
        async for chunk in _iter_lines(proc.stdout):
            now = time.time()
            match = PROGRESS_RE.match(chunk)
            if match:
                # rsync does not always end a progress line before its next message: two of
                # three deletions in a real pull arrived as `…(xfr#0, ir-chk=…)deleting …`.
                # So the progress prefix is peeled off and the rest handled as its own line.
                head, tail = chunk[: match.end()], chunk[match.end():].strip()

                # Throttled into the log, unthrottled into the job: see LOG_PROGRESS_INTERVAL.
                if now - last_log >= LOG_PROGRESS_INTERVAL:
                    last_log = now
                    pending = ""
                    log.write(head.strip() + "\n")
                else:
                    pending = head.strip()
                if track:
                    _apply_progress(job, match)
                if now - last_push >= 0.5:  # ~2 Hz, per the SSE contract
                    last_push = now
                    self._emit(JobEvent("progress", job.id))
                if now - last_term >= 1.0:
                    last_term = now
                    self._append_line(job, head.strip(), "prog")
                if not tail:
                    continue
                chunk = tail
            if chunk.strip():
                text = chunk.rstrip()
                event = FILE_RE.match(text)
                # Everything else goes to the log. A held-back progress line goes first, so the
                # closing summary stays last -- but not before an @-line, or the throttle
                # would put one between every pair of files.
                if pending and event is None:
                    log.write(pending + "\n")
                    pending = ""
                log.write(text + "\n")
                if event is not None:
                    name = event.group("name")
                    if not name.endswith("/"):
                        job.current_file = name
                        self._note_sent(job, name)
                    size = event.group("size")
                    pretty = name
                    if size.isdigit() and not name.endswith("/"):
                        pretty = f"{name}  {int(size):,}"
                    self._append_line(job, pretty, "")
                elif deleted := DELETE_RE.match(text):
                    # A prune, at either end. Directories are not files; the path is kept so
                    # the manifest can retract its claim.
                    name = deleted.group("name")
                    if not name.endswith("/"):
                        job.files_deleted += 1
                        self._note_deleted(job, name)
                    self._append_line(job, text, "warn")
                elif summary := SUMMARY_RE.match(text):
                    if track:
                        job.bytes_wire = sum(
                            int(g.replace(",", "")) for g in summary.groups()
                        )
                    self._append_line(job, text, "info")
                else:
                    lowered = text.lower()
                    css = (
                        "err"
                        if ("error" in lowered or "broken pipe" in lowered
                            or "warning:" in lowered or lowered.startswith("rsync:"))
                        else "info"
                    )
                    self._append_line(job, text, css)

        # The last progress line, so even a short phase logs its final counters.
        if pending:
            log.write(pending + "\n")
        self._flush_lines(job.id)

        code = await proc.wait()
        if abortable:
            self._procs.pop(job.id, None)
        else:
            self._probe_procs.discard(proc)
        return code

    async def _run(self, job_id: int) -> None:
        job = self._live.get(job_id)
        if job is None or job.state not in ("queued", "deferred"):
            return
        lock = self._device_locks.setdefault(job.device_id, asyncio.Lock())
        async with lock, self._admit(job):
            # Again, after the wait: an Abort, a ✕ or a second enqueue may have landed while
            # it waited. Checking only on the way in ran a push the user had cancelled.
            if self._live.get(job_id) is not job or job.state not in ("queued", "deferred"):
                return
            await self._execute(job)

    @contextlib.asynccontextmanager
    async def _admit(self, job: Job):
        """Pushes run together; a pull runs alone.

        A push dereferences the vault as it goes, so beside a pull it can read a blob not yet
        landed (exit 24) or one still in .rsync-partial. A waiting pull holds back pushes
        that arrive after it, or a busy fleet could keep it waiting for ever.
        """
        cond = self._admission
        pull = job.kind == "pull"
        async with cond:
            if pull:
                self._pulls_waiting += 1
                try:
                    await cond.wait_for(lambda: not self._pulling and not self._pushes)
                finally:
                    self._pulls_waiting -= 1
                    cond.notify_all()
                self._pulling = True
            else:
                await cond.wait_for(lambda: not self._pulling and not self._pulls_waiting)
                self._pushes += 1
        try:
            yield
        finally:
            async with cond:
                if pull:
                    self._pulling = False
                else:
                    self._pushes -= 1
                cond.notify_all()

    async def _execute(self, job: Job) -> None:
        job.state = "running"
        job.started_at = time.time()
        job.attempt += 1
        # A retry restarts rsync, and its tallies with it.
        job.files_sent = 0
        job.files_deleted = 0
        self._sent[job.id] = []
        self._deleted[job.id] = []
        self.store.save(job)
        self._emit(JobEvent("dock"))

        log_path = self.settings.logs_dir / f"{job.id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Line buffered, or "Open log" on a running job shows an empty file.
        with log_path.open("w", encoding="utf-8", errors="replace", buffering=1) as log:
            if job.kind == "pull":
                code = await self._run_pull(job, log)
            else:
                if job.catalog:
                    self._phase(job, "1/2 · library", log)
                code = await self._stream(job, job.argv, log)
                # After the files, and never on a dry run.
                if code == 0 and job.catalog and not job.dry_run:
                    await self._replicate_catalog(job, log)

        self._procs.pop(job.id, None)
        job.exit_code = code
        job.finished_at = time.time()
        self._emit(JobEvent("progress", job.id))

        # See _ATTR_PROBLEM_RE. Failing an attrs-only 23 also mis-credited the manifest with
        # `files_sent` names only, and retried a complete transfer twice.
        attrs_only = code == 23 and _attrs_only(log_path)

        if code == 0 or attrs_only:
            job.state = "done"
            job.pct = 100.0
            if attrs_only:
                self._append_line(
                    job,
                    "every file landed; the device would not accept their timestamps "
                    f"— rsync calls that exit {code}",
                    "warn",
                )
                for hint in _hints(log_path, code):
                    self._append_line(job, f"hint: {hint}", "warn")
            if job.dry_run:
                self._append_line(
                    job, "dry run · nothing sent, manifest unchanged", "prog"
                )
            elif job.kind == "pull":
                # `_credit_pull` writes its manifest below, on every outcome; and a pull
                # does not change the upstream's disk usage, so no invalidate_space.
                self._append_line(job, "pull complete", "prog")
            else:
                self._update_manifest(job)
                self.probe.invalidate_space(job.device_id)
                self._append_line(job, "✓ manifest updated", "prog")
        elif code in (15, -15, 143, 20):
            job.state = "aborted"
            job.error = "aborted"
            if job.kind != "pull":
                self._record_partial(job)
        else:
            job.state = "failed"
            # Only the transfer is rsync; a pull's preflight is systemctl.
            what = "rsync" if not job.phase or job.phase.startswith("1/") else job.phase
            job.error = f"{what} exited {code}"
            self._append_line(job, f"{what} error: exit {code}", "err")
            for hint in _hints(log_path, code):
                self._append_line(job, f"hint: {hint}", "warn")
            # Before the retry: the next attempt skips what this one delivered and never
            # names it again.
            if job.kind != "pull":
                self._record_partial(job)
            retries = self._retries_for(job)
            if code == 25:
                # --max-delete refused the prune; a retry meets the same cap.
                self._append_line(
                    job,
                    "not retried: the cap refused the prune, and a retry would meet the "
                    "same one — read the dry run, then raise LIBNODES_PULL_MAX_DELETE if "
                    "the removals are real",
                    "warn",
                )
                retries = 0
            elif code == SPAWN_FAILED:
                self._append_line(
                    job, "not retried: a command that cannot be started will not start "
                    "on the next attempt either", "warn",
                )
                retries = 0
            elif job.kind == "pull" and not job.phase.startswith("1/"):
                # A pull retries only its transfer, where a retry is free; past it,
                # retries would cycle the local service chasing a failure a human must read.
                self._append_line(
                    job,
                    f"not retried: the failure was in {job.phase or 'a later phase'}, "
                    "past the point where a retry is free",
                    "warn",
                )
                retries = 0
            if job.attempt <= retries:
                self._append_line(
                    job, f"--partial kept the transfer · retry {job.attempt}/{retries}", "warn"
                )
                job.state = "queued"
                job.exit_code = None
                job.error = None
                self.store.save(job)
                self._emit(JobEvent("dock"))
                self._queue.put_nowait(job.id)
                return

        if job.kind == "pull" and not job.dry_run:
            # Every outcome: an interrupted pull still received files. It reads the
            # filesystem, not the index, so it may run before the reindex.
            self._credit_pull(job)

        if not job.dry_run:
            # A prune at either end leaves rows claiming files that are gone.
            self._debit(job)

        if job.kind == "pull" and not job.dry_run and self._on_library_changed:
            # Every outcome, abort included: books the index does not know are invisible
            # and unpushable. After the catalog phase, whose titles the index reads.
            # Awaited, so the job ends saying how it went rather than on a line that reads
            # as still in progress -- and, the pull still holding the queue, no push starts
            # against an index that has not heard of the books it brought.
            self._append_line(job, "· reindexing the library", "prog")
            pending = self._on_library_changed()
            if inspect.isawaitable(pending):
                await pending
                meta = self.index.meta()
                if meta.error:
                    self._append_line(job, f"reindex failed: {meta.error}", "warn")
                else:
                    took = f" in {meta.duration:.1f} s" if meta.duration is not None else ""
                    self._append_line(
                        job,
                        f"✓ library reindexed · {meta.entry_count:,} entries{took}",
                        "prog",
                    )

        self.store.save(job)
        self._emit(JobEvent("done", job.id))
        self._emit(JobEvent("dock"))
        self._prune_logs()

    def _retries_for(self, job: Job) -> int:
        device = self.devices.device(job.device_id)
        if device is None:
            return 0
        return device.retries_with(self.devices.config.defaults)

    def _note_sent(self, job: Job, name: str) -> None:
        """Remember a name off an @-line, for `_record_partial` and `_credit_pull`, up to
        SENT_CAP. Past it the entry becomes None, not a list missing its oldest names."""
        if job.kind == "pull" and name.split("/", 1)[0] in SKIP_TOPLEVEL:
            # The vault, Recommended/ and the snapshot are ~24k of a pull's ~45k @-lines,
            # and never a library row.
            return
        sent = self._sent.get(job.id)
        if sent is None:
            return
        if len(sent) >= SENT_CAP:
            self._sent[job.id] = None
            return
        sent.append(name)

    def _note_deleted(self, job: Job, name: str) -> None:
        """Remember a name off a `deleting` line, for `_debit`, under the same cap.
        Unfiltered: retracting a row that never existed costs nothing."""
        gone = self._deleted.get(job.id)
        if gone is None:
            return
        if len(gone) >= SENT_CAP:
            self._deleted[job.id] = None
            return
        gone.append(name)

    def _record_partial(self, job: Job) -> None:
        """Credit an interrupted push with the files it did deliver.

        Truncated to `files_sent`, the `xfr#` count: rsync prints a name when a file
        *starts*, so the last @-line names one in flight, perhaps left truncated by
        --partial -- and a row for it would read as present.
        """
        if job.dry_run:
            return
        names = self._sent.get(job.id)
        if names is None:
            self._append_line(
                job,
                f"too many files to track ({SENT_CAP:,}+) · "
                "manifest not updated, run a scan to resync PRESENT ON",
                "warn",
            )
            return
        delivered = names[: job.files_sent]
        if not delivered:
            return
        recorded: list[tuple] = []
        for path in delivered:
            entry = self.index.entry(path)
            if entry is None or entry.is_dir:
                continue
            recorded.append((entry.path, entry.blob, entry.size, entry.mtime, 0))
        if not recorded:
            return
        self.manifests.record(job.device_id, recorded, source="push")
        self.probe.invalidate_space(job.device_id)
        self._append_line(
            job, f"✓ manifest credited with {len(recorded):,} delivered files", "prog"
        )

    def _credit_pull(self, job: Job) -> None:
        """Credit an upstream with the files it just sent us.

        Not `_update_manifest`, which reads the *local* index: after a pull that describes
        this host, not the far end. A file we received is a file that node has. The
        filesystem decides, not the @-line -- `--partial-dir` keeps an interrupted file out
        of its final name -- which is what makes this safe after an abort. (`files_sent`
        cannot truncate here: the filtered vault lines still counted toward `xfr#`.)
        """
        if job.dry_run:
            return
        names = self._sent.get(job.id)
        if names is None:
            self._append_line(
                job,
                f"too many files to track ({SENT_CAP:,}+) · "
                "manifest not updated, run a scan to resync PRESENT ON",
                "warn",
            )
            return
        root = self.settings.library_root
        recorded: list[tuple] = []
        for path in names:
            full = root / path
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if os.path.islink(full):
                # The book's size through the vault, not the link's own bytes.
                blob = blob_from_link(os.readlink(full))
                try:
                    size = os.stat(full).st_size
                except OSError:
                    size = 0
                recorded.append((path, blob, size, int(st.st_mtime), 0))
            elif os.path.isdir(full):
                recorded.append((path, None, 0, int(st.st_mtime), 1))
            else:
                recorded.append((path, None, st.st_size, int(st.st_mtime), 0))
        if not recorded:
            return
        self.manifests.record(job.device_id, recorded, source="pull")
        self._append_line(
            job,
            f"✓ {job.device_id} credited with {len(recorded):,} files it sent · "
            "a scan still answers for the rest",
            "prog",
        )

    def _debit(self, job: Job) -> None:
        """Retract the manifest rows for what a prune just removed, at either end.

        No filesystem check: rsync prints `deleting` after the unlink. Every outcome but a
        dry run, since an interrupted prune still pruned what it reached. See
        `Manifests.retract` for why a stale row is wrong, not merely untidy.
        """
        if job.dry_run:
            return
        names = self._deleted.get(job.id)
        if names is None:
            self._append_line(
                job,
                f"too many deletions to track ({SENT_CAP:,}+) · "
                "manifest not retracted, run a scan to resync PRESENT ON",
                "warn",
            )
            return
        if not names:
            return
        dropped = self.manifests.retract(job.device_id, names)
        whose = "the upstream" if job.kind == "pull" else "the library"
        self._append_line(
            job,
            f"✓ pruned {job.files_deleted:,} files {whose} no longer has"
            + (f" · {dropped:,} manifest rows retracted" if dropped else ""),
            "prog",
        )

    def _update_manifest(self, job: Job) -> None:
        """Record what the device now holds, so PRESENT ON reflects the push.

        Never what its excludes held back: rsync sent none of it, and a scan keeps push
        rows (`replace_scan`), so a false one stays. Every Full Sync of note9 and s4a once
        recorded Audio/, Video/ and Zhurnaly/, 1,952 files rsync never sent, and the map
        drew both devices at 100% (2026-10-01).
        """
        device = self.devices.config.by_id.get(job.device_id)
        roots = [
            r.path
            for r in self.index.excluded_roots(
                device.excludes_with(self.devices.config.defaults) if device else ()
            )
        ]
        recorded: list[tuple] = []
        for src in job.sources:
            entry = self.index.entry(src)
            if entry is None:
                continue
            recorded.append(_manifest_row(entry))
            if entry.is_dir:
                # The directory too: an empty one leaves no other trace.
                recorded.extend(_manifest_row(e) for e in self.index.subtree(entry.path))
        if roots:
            recorded = [
                row for row in recorded if not any(within(row[0], r) for r in roots)
            ]
        if recorded:
            self.manifests.record(job.device_id, recorded, source="push")

    def _prune_logs(self) -> None:
        try:
            logs = sorted(
                self.settings.logs_dir.glob("*.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for stale in logs[self.settings.log_retention :]:
            try:
                stale.unlink()
            except OSError:
                pass

    # --- deferred watcher -------------------------------------------------

    async def _watch_deferred(self) -> None:
        """Promote a deferred job the moment its node answers, woken by the probe's own
        state changes. The five-minute sweep is only a backstop for a dropped notice."""
        changes = self.probe.subscribe()
        try:
            while True:
                try:
                    flipped = set(await asyncio.wait_for(changes.get(), timeout=300))
                except asyncio.TimeoutError:
                    flipped = None
                try:
                    self._promote(flipped)
                except Exception:  # noqa: BLE001 - the watcher must outlive a bad job
                    log.exception("could not promote deferred jobs")
        finally:
            self.probe.unsubscribe(changes)

    def _promote(self, device_ids: set[str] | None) -> None:
        """Queue every deferred job whose node is online, among `device_ids` (None: any)."""
        for job in list(self._live.values()):
            if job.state != "deferred" or job.hold:
                continue  # held: waits for an explicit Start
            if device_ids is not None and job.device_id not in device_ids:
                continue
            if self.probe.status(job.device_id).online:
                job.state = "queued"
                self.store.save(job)
                self._queue.put_nowait(job.id)
                self._emit(JobEvent("dock"))

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        # First, and synchronously: a death inside a pull's window left the service down.
        hold = self._service_hold
        if hold.exists():
            try:
                unit = json.loads(hold.read_text(encoding="utf-8")).get("unit", "")
            except (OSError, ValueError):
                unit = self.settings.local_service
            if unit:
                subprocess.run(
                    ["systemctl", "--no-ask-password", "start", unit],
                    check=False,
                    capture_output=True,
                )
                log.warning(
                    "restarted %s: a pull had stopped it and this process did not "
                    "survive to start it again",
                    unit,
                )
            hold.unlink(missing_ok=True)

        # A running job cannot resume in place; one that had not started can wait on.
        for job in self.store.unfinished():
            if job.state == "running":
                job.state = "failed"
                job.error = "interrupted by restart"
                job.finished_at = time.time()
                self.store.save(job)
            elif job.id not in self._live:
                self._readopt(job)

        if not self._workers:
            for i in range(max(1, self.settings.concurrency)):
                self._workers.append(
                    asyncio.create_task(self._worker(), name=f"job-worker-{i}")
                )
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.create_task(
                self._watch_deferred(), name="deferred-watcher"
            )

    def _readopt(self, job: Job) -> None:
        """Take back a queued or deferred job the previous process left behind.

        Its argv is rebuilt against the devices.yaml loaded *now*, so a node that has since
        become `upstream` is refused as a retry would be; a job that no longer builds is
        failed with the reason. Ignored, as they once were, they read QUEUED for ever.
        """
        device = self.devices.device(job.device_id)
        config = self.devices.config
        try:
            if device is None:
                raise ValueError(f"{job.device_id} is no longer in devices.yaml")
            if job.kind == "pull":
                argv = build_pull_argv(device, config, self.settings, dry_run=job.dry_run)
            else:
                argv = build_argv(
                    device,
                    config,
                    job.sources,
                    self.settings,
                    dry_run=job.dry_run,
                    adopt=job.adopt,
                    whole_library=job.full_library,
                )
        except ValueError as exc:
            job.state = "failed"
            job.error = f"not resumed after a restart: {exc}"
            job.finished_at = time.time()
            self.store.save(job)
            return
        job.argv = argv
        self.store.save(job)
        self._live[job.id] = job
        self._terms[job.id] = deque(maxlen=self.settings.term_ring)
        self._append_line(job, f"$ {job.command}", "cmd")
        self._append_line(job, "resumed after a restart", "info")
        if job.state == "queued":
            self._queue.put_nowait(job.id)

    async def stop(self) -> None:
        for task in [*self._workers, self._watcher]:
            if task is not None:
                task.cancel()
        for task in [*self._workers, self._watcher]:
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._workers.clear()
        self._watcher = None
        # Readers first, then the children. See procs.reap.
        await reap(self._procs.values())
        self._procs.clear()
        await reap(self._probe_procs)
        self._probe_procs.clear()


# What rsync's diagnostics rarely say: the likely cause, for the failures that actually
# happen on this fleet. A more specific needle comes before a general one it contains.
_HINTS: list[tuple[str, str]] = [
    (
        "no such file or directory",
        "the target's parent directory does not exist on the device — rsync creates "
        "only the last component, and rsync 3.1.x has no --mkpath",
    ),
    ("read-only file system", "the target is mounted read-only on the device"),
    (
        "permission denied (publickey",
        "no usable key for this node — check `identity` and that the key is authorised",
    ),
    ("permission denied", "the ssh user cannot write to the target directory"),
    (
        "connection refused",
        "sshd is not listening on that port — Termux sshd stops when the device sleeps",
    ),
    (
        "no route to host",
        "the DHCP lease may have moved this node; try a hostname instead of an IP",
    ),
    (
        "failed to set times",
        "this target cannot store timestamps — Android's emulated storage (/sdcard) has "
        "no utimensat and refuses it even to root, though a physical card is fine. The "
        "files landed, but every later push will re-send them, because the quick check "
        "compares an mtime that can never match: declare `stores_times: false` for this "
        "device",
    ),
    ("no space left on device", "the device is full"),
    (
        "deletions stopped due to --max-delete",
        "the upstream wanted to remove more than LIBNODES_PULL_MAX_DELETE allows, so "
        "nothing further was deleted — an upstream that is only half mounted looks "
        "exactly like this. Run the Pull dry run and read the `deleting` lines before "
        "raising the cap",
    ),
    (
        "broken pipe",
        "the device dropped mid-transfer — --partial kept what arrived, "
        "so the resume is byte-accurate",
    ),
    (
        "connection unexpectedly closed",
        "the device went away mid-transfer; retry when it is back",
    ),
    (
        "interactive authentication required",
        "polkit refused to let this service manage the unit — install "
        "deploy/50-libnodes-urantia.rules to /etc/polkit-1/rules.d/",
    ),
    ("kex_exchange_identification", "dropbear may not offer a KEX this ssh accepts — "
     "pin one in ~/.ssh/config or in the node's extra ssh options"),
]


def is_attrs_only(text: str) -> bool:
    """True when rsync's exit 23 was about attributes alone, and every byte landed.

    Conservative: at least one diagnostic, and every one a `failed to set`. A vanished
    file, an unreadable book or a full device also exits 23 and must stay a failure.
    """
    problems = _RSYNC_PROBLEM_RE.findall(text or "")
    if not problems:
        return False
    return len(_ATTR_PROBLEM_RE.findall(text)) == len(problems)


def hints_for_text(text: str, code: int) -> list[str]:
    """Likely causes for a failure, from whatever the command said."""
    if code == SPAWN_FAILED:
        # Alone: the spawn error's "No such file or directory" would blame the target.
        return ["a command this job runs is not installed on this host"]
    tail = (text or "")[-4096:].lower()
    found = []
    for needle, hint in _HINTS:
        if needle in tail:
            found.append(hint)
            # Consumed, so "permission denied (publickey)" is not also a write problem.
            tail = tail.replace(needle, "")
    if not found and code == 255:
        found.append("ssh itself failed — the node is probably unreachable")
    return found[:2]


def _hints(log_path: Path, code: int) -> list[str]:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return hints_for_text(text, code)


def _attrs_only(log_path: Path) -> bool:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return is_attrs_only(text)


def _apply_progress(job: Job, match: re.Match) -> None:
    """Read one `--info=progress2` line into the job.

    Three numbers that count three things (measured on an aborted push of 234 files):
    `xfr#N` is transfers *finished* (14 when 15 @-lines had printed); `to-chk=r/t` is
    file-list entries, directories and skipped files included (244 for that directory);
    and the byte count is the running sum of the @-line sizes. The bar tracks entries,
    because rsync's own percentage is bytes over the whole list: a 300 MB repair in a
    10 GB tree reads 3% and never moves.
    """
    raw_bytes, pct, rate, elapsed, xfr, remaining, total = match.groups()
    job.bytes_done = parse_size_token(raw_bytes)
    job.rate = rate
    if xfr:
        # max(): a line buffered from the previous attempt must not drag this one back.
        job.files_sent = max(job.files_sent, int(xfr))
    if total and remaining:
        job.entries_total = int(total)
        job.entries_done = max(0, int(total) - int(remaining))
    if job.entries_total:
        job.pct = min(100.0, job.entries_done * 100 / job.entries_total)
    else:
        job.pct = float(pct)
    # files_total and bytes_total stay as `_estimate` set them: the selection, in files.
    job.eta = _eta(job)


def _eta(job: Job) -> str:
    """Time left in the bar's currency, file-list entries. A byte ETA would divide by the
    whole selection: 10 GB quoted for a 300 MB repair."""
    if not job.entries_total or job.started_at is None:
        return ""
    # Below ~5% the sample is a few directory entries and the extrapolation is nonsense.
    if job.entries_done < max(1, job.entries_total // 20):
        return ""
    elapsed = time.time() - job.started_at
    if elapsed <= 0:
        return ""
    per_sec = job.entries_done / elapsed
    if per_sec <= 0:
        return ""
    remaining = (job.entries_total - job.entries_done) / per_sec
    if remaining <= 0:
        return ""
    h, rem = divmod(int(remaining), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")


async def _iter_lines(stream: asyncio.StreamReader):
    """Yield rsync output split on both \\n and \\r (progress2 rewrites a line in place, so
    readline() would block until the end).

    Decoded incrementally: a read ends wherever 4 KiB does, and decoding each chunk alone
    turned a Cyrillic letter split across it into two U+FFFD, a filename that then matched
    nothing when the manifest was credited.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    while True:
        chunk = await stream.read(4096)
        buffer += decoder.decode(chunk, final=not chunk)
        *lines, buffer = _LINE_BREAK_RE.split(buffer)
        for line in lines:
            if line:
                yield line
        if not chunk:
            break
    if buffer.strip():
        yield buffer


def _manifest_row(entry) -> tuple:
    """An index entry as `Manifests.record` takes it; a directory's recursive size is 0."""
    if entry.is_dir:
        return (entry.path, None, 0, entry.mtime, 1)
    return (entry.path, entry.blob, entry.size, entry.mtime, 0)


def _label_for(sources: Sequence[str]) -> str:
    if not sources:
        return "(nothing)"
    if len(sources) == 1:
        return sources[0] or "(full library)"
    return f"{sources[0]} +{len(sources) - 1} more"


__all__ = [
    "Job",
    "JobEvent",
    "JobRunner",
    "JobState",
    "JobStore",
    "PROGRESS_RE",
    "build_argv",
    "full_sync_sources",
    "build_pull_argv",
    "build_catalog_argv",
    "snapshot_argv",
    "cleanup_argv",
    "service_argv",
    "catalog_rel",
    "REPLICATE_SUFFIX",
    "snapshot_catalog",
    "replicate_catalog_argv",
    "remote_reader_argv",
    "remote_sidecar_argv",
    "PULL_FLAGS",
    "mirror_sources",
]
