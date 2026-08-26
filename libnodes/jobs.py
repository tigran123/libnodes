"""Transfer jobs: the queue, the rsync process, and the progress stream.

Design constraints this module exists to satisfy:

* **Nothing blocks.** A push returns a job id immediately; progress reaches the browser
  over SSE. The only blocking dialog in the app is the offline-push confirmation, and
  that is a template, not a wait.
* **One rsync at a time** (`concurrency`, default 1). The Pi's NIC shares the USB 2.0
  bus with the disk holding the library, so two transfers do not go twice as fast —
  they go half as fast each and spike load.
* **`-L` is mandatory.** The library is symlinks into a content-addressed vault; without
  `--copy-links` a device receives 100-byte dangling links instead of books.
* **Cheap progress.** `--info=progress2` emits one aggregate line rather than a line per
  file, and we throttle what reaches the browser to ~2 Hz.

Durable job history lives in SQLite; the live bits (current file, rate, terminal ring
buffer) stay in memory, with the full transcript on disk under `var/logs/`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Sequence

from .config import SKIP_TOPLEVEL, Settings
from .probe import SERVER_ALIVE_COUNT_MAX, SERVER_ALIVE_INTERVAL, DeviceProbe
from .procs import reap
from .library import LibraryIndex
from .manifests import Manifests
from .models import Device, DevicesFile

JobState = Literal["queued", "running", "done", "failed", "aborted", "deferred"]

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
  adopt       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_jobs_state ON jobs(state);
"""

# The byte column has two formats and the default flags produce the second one:
#   plain  (-P)     `        1,234,567  45%   11.34MB/s    0:00:12 (xfr#3, to-chk=9/12)`
#   human  (-avhP)  `           734.38K   1%  334.56MB/s    0:00:00 (xfr#1, to-chk=13/16)`
# `-h` is in `defaults.rsync_flags`, so matching only the plain form would silently
# report 0 bytes for every real transfer.
PROGRESS_RE = re.compile(
    r"^\s*([\d,]+(?:\.\d+)?[KMGTP]?)\s+(\d+)%\s+(\S+)\s+(\d+:\d\d:\d\d)"
    r"(?:\s+\(xfr#(\d+),\s+(?:ir-chk|to-chk)=(\d+)/(\d+)\))?"
)

# The closing line `stats1` buys us, and the only place rsync says what actually crossed
# the network:
#   `sent 2,491,047 bytes  received 4,203,364 bytes  25,997.71 bytes/sec`
# The progress counter above is the *logical* size of the files rsync handled; when the
# delta algorithm matches an existing copy, the two differ by orders of magnitude. A real
# push of 98 files reported 4,379,115,438 bytes against 6.7 MB on the wire — `speedup is
# 1,482.40`. Both numbers are true and only one of them is what the link carried.
SUMMARY_RE = re.compile(r"^sent ([\d,]+) bytes\s+received ([\d,]+) bytes")

# rsync exit 23 is "some files/attrs were not transferred" -- one code for two outcomes
# that could not be further apart. Every diagnostic it emits is a line starting `rsync:`,
# so the two are separable: if all of them are attribute failures then no file's *data*
# was missed, and the push delivered everything it was asked to.
#
# This is not a corner case, it is the standing outcome for any target on Android's
# *emulated* storage. `/sdcard` is not a filesystem but a FUSE shim (`/dev/fuse`) with
# nothing underneath, and its daemon does not implement utimensat: EPERM to everyone, root
# included. Measured on nexus10 (Android 5.1) -- `touch -t` fails there as root, and job #1
# delivered both files byte-exact (5,117,977 and 1,002,176, verified with stat on the
# device) and still exited 23 with nothing but two `failed to set times` lines. It was
# drawn as a red TRANSFER FAILED. A physical card is the other case and does not do this:
# see Device.stores_times.
#
# The role tag is optional because it is the *receiver* that reports this, and the rsync
# on the far side is whatever the device ships: 3.2+ prints `rsync: [generator] failed to
# set times on ...`, older builds print `rsync: failed to set times on ...`.
_RSYNC_PROBLEM_RE = re.compile(r"^rsync: ", re.MULTILINE)
_ATTR_PROBLEM_RE = re.compile(
    r"^rsync: (?:\[[^\]]+\] )?failed to set \w+", re.MULTILINE
)

_MULT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
_SIZE_TOKEN_RE = re.compile(r"^([\d,]+(?:\.\d+)?)([KMGTP]?)$")

# We tell rsync exactly how to announce each file rather than guessing which of its
# lines is a filename. `--out-format` takes escapes (see the log format section of
# rsyncd.conf(5)): %i itemized change flags, %l length, %n name. The leading marker is
# ours, so a file event can never be confused with progress output or a summary line —
# which a filename with a % or a percentage in it otherwise could be.
#
#   @>f+++++++++|3000000|Science/Chess/Tal.pdf
#   @cd+++++++++|4096|Science/Chess/
#
# %i is deliberately NOT included. The manpage warns that adding it "increases the
# logging of names to mention any item that is changed in any way" — and on a FAT target
# every file differs in permissions for ever, so a no-op run logged 24,616 lines instead
# of nothing, and one dry run wrote 2.3 MB. Without it rsync mentions only genuine
# updates. Directories are recognisable by their trailing slash.
OUT_FORMAT = "@%l|%n"
FILE_RE = re.compile(r"^@(?P<size>\d*)\|(?P<name>.*)$")

# Suppress what we do not parse. flist0 drops "sending incremental file list", misc0 the
# housekeeping chatter; stats1 keeps the closing summary, which is worth having in the
# log. These come after the user's own flags, so they win over -v.
INFO_FLAGS = "progress2,flist0,misc0,stats1"

#: The transfer flags are LibNodes', not the user's. The program depends on their exact
#: effect: -L because the library is symlinks into a CAS vault, -R so a source keeps its
#: path on the device, --info/--out-format because the progress parser reads them. A
#: hand-edited devices.yaml that dropped one would break the app in ways that look like
#: bugs rather than misconfiguration, so `rsync_flags` is no longer a config key.
#:
#: -L is the one member that is conditional, and only on a whole different kind of target:
#: a `sync_mode: mirror` node wants the symlinks *kept*, so `build_argv` drops it there.
#: For every reader -- which is every device this program had until then -- it is
#: mandatory. Dropping it is only safe together with the other half of that mode: a mirror
#: sends the whole root, so .data/ travels with the links and they still resolve on the far
#: side. See Device.sync_mode.
#:
#: -h is absent on purpose: it exists to make rsync's own output pleasant for a human
#: reading a terminal, and we format every number for display ourselves. Plain byte
#: counts are one less thing to parse.
#:
#: -O (--omit-dir-times) is there for the log, not the transfer. Without it rsync wants
#: to stamp every directory's mtime, counts each as "touched", and reports it: a dry run
#: against an already-synced device listed 3,839 directories around the 4 files that
#: actually mattered. Directory timestamps are meaningless on a FAT card anyway.
BASE_FLAGS = ["-a", "-O", "--partial", "-L", "-R"]

#: How many delivered filenames one job may hold in memory, so an interrupted push can
#: still tell the manifest what landed (`_note_sent`). The whole library is 24,616 files;
#: this clears that with room to spare, at ~60 bytes a path.
SENT_CAP = 50_000


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
    #: Transfers rsync *completed*, from the highest `xfr#N` it reported. Not the count
    #: of @-lines: rsync prints the name when it starts a file, so the last @-line of an
    #: interrupted run names a file that never landed.
    files_sent: int = 0
    #: Files in the selection, from the index. Set once at submit and never overwritten,
    #: so the denominator means the same thing in the dock, the file table and the
    #: manifest.
    files_total: int = 0
    #: File-list entries rsync has walked past, directories included. `to-chk` counts
    #: `Audio/` and its 9 subdirectories alongside its 234 files, which is why this is
    #: 244 where files_total is 234 — and why the two must never share a widget.
    entries_done: int = 0
    entries_total: int = 0
    #: Logical size of the files rsync has handled — progress2's counter, which is the
    #: running sum of the @%l sizes (measured: 259,124,497 at xfr#14, byte-for-byte the
    #: 14 preceding @ sizes). Skipped files contribute nothing. It is *not* what the
    #: network carried: see bytes_wire.
    bytes_done: int = 0
    bytes_total: int = 0
    #: What actually crossed the link, from rsync's closing `sent … received …` line, so
    #: it only exists once the job has finished. Delta-matching makes this far smaller
    #: than bytes_done whenever the device already holds a copy — 6.7 MB against
    #: 4,379,115,438 on one measured push.
    bytes_wire: int = 0
    pct: float = 0.0
    exit_code: int | None = None
    error: str | None = None
    argv: list[str] = field(default_factory=list)
    attempt: int = 0
    dry_run: bool = False
    #: Deferred, but the user asked us NOT to start it automatically. The watcher
    #: leaves these alone; they wait for an explicit Start.
    hold: bool = False
    #: An adoption run: reconcile metadata for files the device already has, moving no
    #: data. Recorded so the Jobs table can label it and history stays truthful.
    adopt: bool = False

    # Live-only, never persisted.
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
        return " ".join(shlex.quote(a) for a in self.argv)

    @property
    def state_badge(self) -> str:
        return {
            "running": "badge-accent",
            "queued": "badge",
            "deferred": "badge-warn",
            "done": "badge-ok",
            "failed": "badge-err",
            "aborted": "badge-err",
        }[self.state]


@dataclass
class JobEvent:
    """A change worth pushing to the browser. Rendered to HTML by the route layer."""

    kind: Literal["progress", "line", "done", "dock", "devices"]
    job_id: int | None = None
    text: str = ""
    css: str = ""


class JobStore:
    """Durable job history. The design shows past jobs, so this survives a restart."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            # Additive migrations for databases created by an earlier version. SQLite
            # has no ADD COLUMN IF NOT EXISTS, so ask first.
            #
            # files_sent/entries_* replaced a single files_done that conflated "files
            # transferred" with "file-list entries examined". The old column is left
            # alone rather than migrated: its rows hold the entry count, and quietly
            # relabelling those as transfers is the exact confusion being fixed here.
            existing = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            for column, ddl in (("hold", "INTEGER DEFAULT 0"),
                                ("adopt", "INTEGER DEFAULT 0"),
                                ("files_sent", "INTEGER DEFAULT 0"),
                                ("entries_done", "INTEGER DEFAULT 0"),
                                ("entries_total", "INTEGER DEFAULT 0"),
                                ("bytes_wire", "INTEGER DEFAULT 0")):
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
                    "hold, adopt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    "files_sent=?, files_total=?, entries_done=?, entries_total=?, "
                    "bytes_done=?, bytes_total=?, bytes_wire=?, pct=?, "
                    "exit_code=?, error=?, argv=?, attempt=?, dest=?, hold=? "
                    "WHERE id=?",
                    (
                        job.state,
                        job.started_at,
                        job.finished_at,
                        job.files_sent,
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
        hold=bool(row["hold"] if "hold" in row.keys() else 0),
        adopt=bool(row["adopt"] if "adopt" in row.keys() else 0),
    )


def build_argv(
    device: Device,
    config: DevicesFile,
    sources: Sequence[str],
    settings: Settings,
    dry_run: bool = False,
    adopt: bool = False,
) -> list[str]:
    """Compose the rsync command as an argv list — never a shell string.

    Run with `cwd=library_root` and `-R`, so a source of `Science/Philology/` lands at
    `<target>/Science/Philology/` and several sources can share one invocation while
    keeping the library's shape on the device.

    The mode is read off the device rather than passed in, so every caller — including the
    three preview renders in `routes/devices.py` — gets it without opting in.
    """
    defaults = config.defaults
    mirror = device.is_mirror

    if mirror:
        # Both guards exist because of --delete below. In books mode a wrong source list
        # transfers the wrong thing; here it *removes* the right thing, so neither case
        # may be allowed to reach rsync.
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

    # LibNodes owns the transfer flags. See BASE_FLAGS.
    argv = [
        "rsync",
        # A mirror keeps the CAS shape, so the one flag that translates it comes out. See
        # BASE_FLAGS: this is the only place -L is ever absent, and it is only safe because
        # the source below is the whole root, vault included.
        *(f for f in BASE_FLAGS if not (mirror and f == "-L")),
        f"--info={INFO_FLAGS}",
        f"--out-format={OUT_FORMAT}",
    ]
    if dry_run:
        argv.append("-n")

    # A replica that keeps what the Pi dropped is not a replica. Deliberately kept under
    # -n as well: a mirror dry run is the only way to read what a prune would remove
    # before it removes it, which makes it the safety feature rather than the hazard.
    #
    # Not on an adopt. That run exists to repair timestamps on files already in place --
    # pairing "change nothing" with "delete whatever does not match" would be a trap.
    if mirror and not adopt:
        argv.append("--delete")

    # The target filesystem decides, not the device type: an ext4 Linux node keeps full
    # archive semantics, a FAT card does not get chmod attempts it can only fail.
    if not device.fs_profile.perms:
        argv.append("--no-perms")

    # FAT stores the seconds field in units of two, so a timestamp rsync wrote reads back
    # up to a second earlier and rsync's exact comparison calls the file changed. Left
    # uncompensated this re-sent 8,786 of 24,620 files on every push to a real FAT32 SD
    # card; --modify-window=1 took that to 0. It is per-filesystem for the same reason
    # --no-perms is: on ext4 the timestamps are exact and worth comparing exactly.
    if device.fs_profile.modify_window:
        argv.append(f"--modify-window={device.fs_profile.modify_window}")

    if adopt:
        # Adoption: the device already holds the files, but they carry the mtimes of
        # however they were copied there, so rsync's default size+mtime check wants to
        # re-send all of them. --size-only makes it trust matching sizes and skip the
        # transfer, while -a still repairs the mtimes on those skipped files. Measured
        # against a real device: 51.6 MB / 14 files reconciled in 0.66s, 0 bytes moved,
        # after which an ordinary sync itemises nothing at all.
        #
        # Only --size-only. Permissions are the filesystem's business, decided above:
        # forcing --no-perms here too would quietly weaken an adopt onto ext4, where
        # the permissions are real and worth repairing.
        argv.append("--size-only")

    # A target that cannot store an mtime at all — Android's emulated storage, declared
    # with `stores_times: false` (a path fact, not a platform one: see Device.stores_times,
    # where the physical-card case that works is written down beside it). Both flags,
    # because neither works alone and each fixes a different half of the same fact.
    # Measured on nexus10 against files byte-identical to the source, with `-n -i`:
    #
    #   -a                 <f..t......   re-sends every push   exit 23
    #   -a --no-times      <f..T......   re-sends every push   exit 0
    #   -a --size-only     .f..t......   sends nothing         exit 23
    #   -a --size-only --no-times        sends nothing         exit 0
    #
    # `<` is data on its way; `.` is nothing sent. --size-only stops rsync comparing an
    # mtime that can never match, and --no-times stops it then trying to write one it can
    # never write — which is what the remaining exit 23 in row three is. Only the pair
    # gives a clean run, which is why this is not two independent settings.
    #
    # --size-only is Adopt's alone everywhere else, and deliberately so: sizes usually
    # match precisely *because* content diverged in place. The exception is confined to a
    # node that has declared it cannot store the alternative, and the library being
    # content-addressed is what makes it affordable — a changed book gets a new blake2b
    # blob, and `scan`/Adopt compares those hashes rather than sizes.
    #
    # --modify-window is still emitted above and is inert here, times being uncompared.
    # It stays: it is a fact about the filesystem, this is a fact about the mount, and
    # tangling the two would make each harder to reason about than the dead flag is.
    if not device.stores_times:
        argv.append("--no-times")
        if not adopt:
            # Adopt has already added it, and rsync would take it twice happily enough;
            # an argv that says a thing once is easier to read in the log header.
            argv.append("--size-only")

    bandwidth = device.bandwidth_with(defaults)
    if bandwidth:
        argv.append(f"--bwlimit={bandwidth}")
    for pattern in device.excludes_with(defaults):
        argv.append(f"--exclude={pattern}")

    timeout = device.timeout_with(defaults)
    ssh_bits = ["ssh", "-p", str(device.effective_port)]
    if device.identity:
        ssh_bits += ["-i", str(device.identity)]
    ssh_bits += [
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={min(timeout, 30)}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        # The same keepalives the probe uses, and for the same reason: both ride the one
        # multiplexed master the Pi's ssh config creates per device, so a transfer that
        # disagreed with the probe about it would either inherit the probe's settings
        # anyway (whoever opened the master wins) or wedge behind a dead one. Only fires
        # on total silence, which a running transfer never produces. See ssh_argv.
        "-o",
        f"ServerAliveInterval={SERVER_ALIVE_INTERVAL}",
        "-o",
        f"ServerAliveCountMax={SERVER_ALIVE_COUNT_MAX}",
    ]
    extra = device.effective_ssh_options
    if extra:
        ssh_bits += shlex.split(extra)
    argv += ["-e", " ".join(shlex.quote(b) if " " in b else b for b in ssh_bits)]

    root = Path(settings.library_root)
    if mirror:
        # One source, the root itself, and it has to be this way round: --delete only
        # prunes directories that are part of the transfer. Hand rsync the top-level names
        # and it cleans inside each of them while never once scanning the destination root,
        # so a file or a whole directory that exists only on the device outlives every
        # replicate. `./` makes the transfer root the destination root, which is what a
        # replica means. `.` rather than `""`: an empty relative source appends a bare "/"
        # and defeats -R.
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
    """Every top-level entry, with nothing held back. The mirror counterpart.

    Three differences from `full_sync_sources`, and each is the point rather than an
    oversight:

    * **No SKIP_TOPLEVEL.** `.data/` stops being optional and becomes mandatory — a
      mirror keeps the symlinks, so without the vault beside them every one of them
      dangles, which is the exact failure `-L` exists to prevent, reached from the other
      side. `urantia-library/` goes because a replica of the Pi's /Books is what was
      asked for; that is the mode's whole cost, and it is confined to nodes that name it.
    * **`Recommended/` too.** It is skipped for a reader *because* `-L` would expand its
      companion symlinks into a second full copy of every recommended book. Preserved as
      links they cost a few hundred bytes, so the reason not to send it does not apply.
    * **Files as well as directories**, so CLAUDE.md, exclude.txt and the dotfiles at the
      root are replicated rather than silently dropped.

    Note what these names are *for*. They are the job's logical sources — what
    `_estimate` prices, what `_update_manifest` records, what the row says was pushed —
    and not what rsync is handed. `build_argv` passes a mirror one source, `./`, because
    `--delete`'s scope is the directories in the transfer: with the names enumerated,
    rsync prunes inside `Science/` but never looks at the destination root, so a stray
    top-level file survives every replicate for ever. Measured on a local pair — an
    enumerated run left `Leftover.pdf` and a whole orphaned `OldCat/` in place, `./`
    removed both. An empty list here therefore still means "refuse", because the guard is
    about whether we know what the library holds, not about argv length.

    Sorted, so the job's source list is stable between runs and diffable in the log.
    """
    root = Path(settings.library_root)
    try:
        return sorted(e.name for e in os.scandir(root))
    except OSError:
        # build_argv refuses an empty mirror source list rather than running --delete
        # against nothing. Returning [] here is what hands it that decision.
        return []


class JobRunner:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        index: LibraryIndex,
        manifests: Manifests,
        probe: DeviceProbe,
        devices,
    ) -> None:
        self.settings = settings
        self.store = store
        self.index = index
        self.manifests = manifests
        self.probe = probe
        self.devices = devices

        self._live: dict[int, Job] = {}
        self._terms: dict[int, deque[tuple[str, str]]] = {}
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        #: Names off the @-lines, in rsync's order, so a run that dies part-way can still
        #: say which files it delivered. Bounded by SENT_CAP; `None` marks a job that
        #: overflowed and whose list is therefore no longer a complete prefix.
        self._sent: dict[int, list[str] | None] = {}
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._subs: set[asyncio.Queue] = set()
        self._watcher: asyncio.Task | None = None

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
                # A stalled reader must not stall the transfer.
                pass

    def _append_line(self, job: Job, text: str, css: str = "") -> None:
        ring = self._terms.setdefault(
            job.id, deque(maxlen=self.settings.term_ring)
        )
        ring.append((css, text))
        self._emit(JobEvent("line", job.id, text=text, css=css))

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
    ) -> Job:
        config = self.devices.config
        argv = build_argv(
            device, config, sources, self.settings, dry_run=dry_run, adopt=adopt
        )
        files_total, bytes_total = self._estimate(sources, mirror=device.is_mirror)

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
        self, sources: Sequence[str], *, mirror: bool = False
    ) -> tuple[int, int]:
        """Pre-flight totals from the index, so the dock has numbers before rsync does.

        Sources the index does not know contribute nothing, which is silent by design --
        most of them are simply not there. A mirror is the case where that silence would
        mislead: it sends `.data/` and `urantia-library/` as paths, and neither is indexed.

        For a mirror the two numbers therefore need opposite corrections. The *bytes* are
        already close, because a symlink's indexed size is the blob it dereferences to --
        exactly what moves -- so counting the vault again would double it; the vault is
        added for its *file count* only, and its bytes are the same bytes. Without that
        count the bar would claim ~24.6k files for a run rsync sees as roughly twice that,
        and a count that is not the number of files is the thing CLAUDE.md forbids.
        """
        files = 0
        size = 0
        for src in sources:
            entry = self.index.entry(src)
            if entry is None:
                continue
            if entry.is_dir:
                files += entry.files or 0
                size += entry.size
            else:
                files += 1
                size += entry.size
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
        """Drop a job from the dock, leaving its history row alone.

        Works for unfinished jobs too — a deferred job whose device is never coming
        back must be removable, and refusing to dismiss it (as this once did) leaves a
        card the user cannot get rid of.
        """
        job = self._live.get(job_id)
        if job is None:
            return
        if not job.finished:
            job.state = "aborted"
            job.finished_at = time.time()
            job.error = job.error or "dismissed"
            self.store.save(job)
        self._live.pop(job_id, None)
        self._terms.pop(job_id, None)
        self._sent.pop(job_id, None)
        self._emit(JobEvent("dock"))

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
            self._live.pop(job_id, None)
            self._terms.pop(job_id, None)
            self._sent.pop(job_id, None)

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

    async def _run(self, job_id: int) -> None:
        job = self._live.get(job_id)
        if job is None or job.state not in ("queued", "deferred"):
            return

        job.state = "running"
        job.started_at = time.time()
        job.attempt += 1
        # A retry restarts rsync from scratch, so both the xfr# counter and the list of
        # delivered names start over with it. --partial means the retry is cheap, not
        # that the previous attempt's tally still applies.
        job.files_sent = 0
        self._sent[job.id] = []
        self.store.save(job)
        self._emit(JobEvent("dock"))

        log_path = self.settings.logs_dir / f"{job.id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                *job.argv,
                cwd=str(self.settings.library_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            job.state = "failed"
            job.error = str(exc)
            job.finished_at = time.time()
            self.store.save(job)
            self._append_line(job, str(exc), "err")
            self._emit(JobEvent("done", job.id))
            self._emit(JobEvent("dock"))
            return

        self._procs[job.id] = proc
        last_push = 0.0
        last_term = 0.0

        # buffering=1 (line buffered). Without it Python holds up to 8 KB, so "Open log"
        # on a running job showed an empty file — which is exactly when you most want to
        # read it. A long transfer wrote nothing to disk until it finished.
        with log_path.open(
            "w", encoding="utf-8", errors="replace", buffering=1
        ) as log:
            log.write(f"$ {job.command}\n")
            assert proc.stdout is not None
            async for chunk in _iter_lines(proc.stdout):
                log.write(chunk + "\n")
                now = time.time()
                match = PROGRESS_RE.match(chunk)
                if match:
                    _apply_progress(job, match)
                    if now - last_push >= 0.5:  # ~2 Hz, per the SSE contract
                        last_push = now
                        self._emit(JobEvent("progress", job.id))
                    if now - last_term >= 1.0:
                        last_term = now
                        self._append_line(job, chunk.strip(), "prog")
                elif chunk.strip():
                    text = chunk.rstrip()
                    event = FILE_RE.match(text)
                    if event is not None:
                        # A file or directory rsync actually touched. Directories carry
                        # a trailing slash and are noise in the "current file" readout.
                        name = event.group("name")
                        if not name.endswith("/"):
                            job.current_file = name
                            self._note_sent(job, name)
                        size = event.group("size")
                        pretty = name
                        if size.isdigit() and not name.endswith("/"):
                            pretty = f"{name}  {int(size):,}"
                        self._append_line(job, pretty, "")
                    elif summary := SUMMARY_RE.match(text):
                        # rsync's closing tally, and the only figure here that is bytes
                        # on the wire rather than bytes of file.
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

            code = await proc.wait()

        self._procs.pop(job.id, None)
        job.exit_code = code
        job.finished_at = time.time()
        self._emit(JobEvent("progress", job.id))

        # See _ATTR_PROBLEM_RE: exit 23 says "files/attrs", and when it is only the attrs
        # the transfer did everything a transfer is for. Marking it failed cost more than
        # a wrong colour -- a failed job takes _record_partial, which credits the manifest
        # with `files_sent` names only, so a push that delivered a whole directory was
        # recorded as whatever rsync happened to have counted when it gave up.
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
                # This line used to sit outside the guard, so a dry run signed off with
                # "manifest updated" having updated nothing — the one job that cannot
                # change a device claiming it had recorded one. PRESENT ON then stayed
                # put, which reads as the manifest being broken rather than untouched.
                self._append_line(
                    job, "dry run · nothing sent, manifest unchanged", "prog"
                )
            else:
                self._update_manifest(job)
                self.probe.invalidate_space(job.device_id)
                # "· index re-scanned" was in this line too and nothing ever re-scanned
                # it: the library index is rebuilt on its own schedule (library.py) and
                # never by a job. A push records what the device now holds; that is all.
                self._append_line(job, "✓ manifest updated", "prog")
        elif code in (15, -15, 143, 20):
            job.state = "aborted"
            job.error = "aborted"
            self._record_partial(job)
        else:
            job.state = "failed"
            job.error = f"rsync exited {code}"
            self._append_line(job, f"rsync error: exit {code}", "err")
            for hint in _hints(log_path, code):
                self._append_line(job, f"hint: {hint}", "warn")
            # Before the retry, not after it: each attempt starts rsync from scratch, so
            # attempt 2 skips what attempt 1 delivered and never names those files again.
            # Credit them now or lose them.
            self._record_partial(job)
            retries = self._retries_for(job)
            if job.attempt <= retries:
                # --partial is in the default flags, so the retry resumes byte-accurate.
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
        """Remember a name off an @-line, for `_record_partial`.

        Bounded because this is per-job memory, but generously: the whole library is
        24,616 files, so SENT_CAP holds the worst real case at roughly 3 MB of strings.
        Overflow sets the entry to None rather than dropping the oldest name — a
        truncated list is no longer a prefix of what rsync sent, and a manifest is worth
        nothing if it is only mostly right about which files exist.
        """
        sent = self._sent.get(job.id)
        if sent is None:
            return
        if len(sent) >= SENT_CAP:
            self._sent[job.id] = None
            return
        sent.append(name)

    def _record_partial(self, job: Job) -> None:
        """Credit an interrupted push with the files it did deliver.

        Without this, aborting a transfer discards everything it achieved: a run that
        placed 14 of 234 files left PRESENT ON reading the pre-push count, so the UI
        described a device state that had not been true for hours.

        Truncated to `files_sent` — the `xfr#` count — because rsync prints a file's name
        when it *starts* sending it. The trailing @-line of an interrupted run names the
        file that was in flight, which `--partial` may well have left on the device
        truncated under its final name. Recording that one would be worse than recording
        nothing: a size-mismatched row still reads as present.
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
            # Blob and size come from the index, exactly as the clean-exit path takes
            # them, so a partial row is as trustworthy as a whole-push row.
            recorded.append((entry.path, entry.blob, entry.size, entry.mtime, 0))
        if not recorded:
            return
        self.manifests.record(job.device_id, recorded, source="push")
        self.probe.invalidate_space(job.device_id)
        self._append_line(
            job, f"✓ manifest credited with {len(recorded):,} delivered files", "prog"
        )

    def _update_manifest(self, job: Job) -> None:
        """Record what the device now holds, so PRESENT ON reflects the push."""
        recorded: list[tuple] = []
        for src in job.sources:
            entry = self.index.entry(src)
            if entry is None:
                continue
            if entry.is_dir:
                # The directory itself as well as its contents: rsync -R creates it, and
                # an empty one leaves no other trace that it made it across.
                recorded.append((entry.path, None, 0, entry.mtime, 1))
                recorded.extend(self._descend_manifest(src))
            else:
                recorded.append((entry.path, entry.blob, entry.size, entry.mtime, 0))
        if recorded:
            self.manifests.record(job.device_id, recorded, source="push")

    def _descend_manifest(self, path: str) -> list[tuple]:
        out: list[tuple] = []
        for child in self.index.children(path, limit=20000):
            if child.is_dir:
                out.append((child.path, None, 0, child.mtime, 1))
                out.extend(self._descend_manifest(child.path))
            else:
                out.append((child.path, child.blob, child.size, child.mtime, 0))
        return out

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
        """Promote deferred jobs the moment their node answers."""
        while True:
            await asyncio.sleep(60)
            try:
                for job in list(self._live.values()):
                    if job.state != "deferred" or job.hold:
                        # `hold` is the user having unticked "run automatically"; such a
                        # job waits for an explicit Start, however reachable the node is.
                        continue
                    if self.probe.status(job.device_id).online:
                        job.state = "queued"
                        self.store.save(job)
                        self._queue.put_nowait(job.id)
                        self._emit(JobEvent("dock"))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        # A job that was running when the process died cannot be resumed in place;
        # mark it failed so the history is honest and the user can retry.
        for job in self.store.unfinished():
            if job.state == "running":
                job.state = "failed"
                job.error = "interrupted by restart"
                job.finished_at = time.time()
                self.store.save(job)

        if not self._workers:
            for i in range(max(1, self.settings.concurrency)):
                self._workers.append(
                    asyncio.create_task(self._worker(), name=f"job-worker-{i}")
                )
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.create_task(
                self._watch_deferred(), name="deferred-watcher"
            )

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
        # Readers first, rsync second. This used to call terminate() and return, which
        # only asks the child to go and leaves its transport open for a garbage collector
        # that runs after the loop has closed. See procs.reap.
        await reap(self._procs.values())
        self._procs.clear()


# rsync's own diagnostics are accurate but rarely name the actual cause. These are the
# failures that actually happen against e-readers and phones, each paired with the thing
# worth checking. Mirrors the `hint:` lines the design shows in the connection-test strip.
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
        "broken pipe",
        "the device dropped mid-transfer — --partial kept what arrived, "
        "so the resume is byte-accurate",
    ),
    (
        "connection unexpectedly closed",
        "the device went away mid-transfer; retry when it is back",
    ),
    ("kex_exchange_identification", "dropbear may not offer a KEX this ssh accepts — "
     "pin one in ~/.ssh/config or in the node's extra ssh options"),
]


def is_attrs_only(text: str) -> bool:
    """True when rsync's exit 23 was about attributes alone, and every byte landed.

    Deliberately conservative: it needs at least one diagnostic (an exit 23 with none at
    all is not something we understand, so it stays a failure) and *every* one of them
    has to be an attribute failure. A vanished source file, an unreadable book, a full
    device -- each also exits 23, each prints an `rsync:` line that is not `failed to
    set`, and each is a genuinely partial transfer that must keep saying so.
    """
    problems = _RSYNC_PROBLEM_RE.findall(text or "")
    if not problems:
        return False
    return len(_ATTR_PROBLEM_RE.findall(text)) == len(problems)


def hints_for_text(text: str, code: int) -> list[str]:
    """Likely causes for a failure, from whatever the command said."""
    tail = (text or "")[-4096:].lower()
    found = [hint for needle, hint in _HINTS if needle in tail]
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

    Three numbers, three different meanings, and the bug this function is the fix for
    was reading them as one. Measured against a real aborted push of 234 files:

    * `xfr#N` is the count of transfers rsync has **finished** — 14 when the log already
      held 15 `@` lines, because the name is printed when a file starts.
    * `to-chk=r/t` counts **file-list entries**, directories included and skipped files
      included: 244 for a directory holding 234 files and 9 subdirectories. It walked
      35 of them while sending 15, so reporting it as "35 files" was a straight
      overstatement of the work done.
    * the leading byte count is the running sum of the `@%l` sizes — 259,124,497 at
      `xfr#14`, exactly the 14 preceding sizes added up. Skipped files contribute
      nothing to it, so it needs no adjustment.

    rsync's own percentage is bytes-sent over the size of the whole file list, so a
    300 MB repair inside a 10 GB tree reads 3% and never advances. The bar tracks
    entries instead, which is the one quantity that always reaches its total.
    """
    raw_bytes, pct, rate, elapsed, xfr, remaining, total = match.groups()
    job.bytes_done = parse_size_token(raw_bytes)
    job.rate = rate
    if xfr:
        # max() rather than assignment: _run zeroes the counter at the start of each
        # attempt, and a progress line buffered from the previous rsync must not be able
        # to drag the new attempt's count backwards.
        job.files_sent = max(job.files_sent, int(xfr))
    if total and remaining:
        job.entries_total = int(total)
        job.entries_done = max(0, int(total) - int(remaining))
    if job.entries_total:
        job.pct = min(100.0, job.entries_done * 100 / job.entries_total)
    else:
        job.pct = float(pct)
    # files_total and bytes_total stay as _estimate set them, from the index. They are
    # the size of what was *selected*, and the queued card, the dock and the jobs table
    # all quote them; letting rsync redefine them mid-run is what made one directory
    # read 234 files in the file table and 244 in the dock.
    job.eta = _eta(job)


def _eta(job: Job) -> str:
    """Time left, in the same currency as the bar: file-list entries.

    Estimating from bytes needs a byte total, and the only honest one available mid-run
    is the whole selection — which for a mostly-synced tree is orders of magnitude more
    than will actually move (10 GB quoted for a 300 MB repair). Entries are what the bar
    counts down, so the ETA counts the same thing down at the observed rate.
    """
    if not job.entries_total or job.started_at is None:
        return ""
    # Below ~5% the sample is a handful of directory entries consumed in milliseconds
    # and the extrapolation is nonsense. Say nothing rather than something wrong.
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


async def _iter_lines(stream: asyncio.StreamReader):
    """Yield rsync output split on both \\n and \\r.

    `--info=progress2` rewrites one line in place with carriage returns, so a plain
    readline() would block until the transfer finished.
    """
    buffer = ""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        buffer += chunk.decode("utf-8", errors="replace")
        buffer = buffer.replace("\r\n", "\n")
        while True:
            idx = min(
                (i for i in (buffer.find("\n"), buffer.find("\r")) if i != -1),
                default=-1,
            )
            if idx == -1:
                break
            line, buffer = buffer[:idx], buffer[idx + 1 :]
            if line:
                yield line
    if buffer.strip():
        yield buffer


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
    "mirror_sources",
]
