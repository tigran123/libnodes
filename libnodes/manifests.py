"""What is actually on each device.

Three things write here, tagged in `source`: a push (what rsync shipped), a pull (what an
upstream sent us) and a scan (what the device listed). Staleness is decided on the blob
hash where there is one: in a content-addressed library a different hash is a different
book.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Literal, Sequence

from .library import Entry, held_back, within

if TYPE_CHECKING:
    from .models import Device

Presence = Literal["ok", "stale", "partial", "absent"]

#: The coverage map's answers. `absent` is a claim -- a scan listed the device and this
#: was not on it -- and `unknown` is the lack of one; see `CoverageRow.state`. `share` is
#: all a device's excludes let it take, and `excluded` is what they hold back entirely.
Coverage = Literal["complete", "share", "partial", "excluded", "absent", "unknown"]

#: The widest folder matrix the coverage map draws: 32 columns of 20px beside the ~430px
#: of device columns is the 1090px band, and covers 385 of the library's 407 directories
#: (2026-09-29). Past it (Fiction has 304) the map draws the bars alone.
MAX_COLUMNS = 32

SCHEMA = """
CREATE TABLE IF NOT EXISTS manifest (
  device_id  TEXT NOT NULL,
  path       TEXT NOT NULL,
  blob       TEXT,
  size       INTEGER,
  mtime      INTEGER,
  shipped_at REAL,
  source     TEXT NOT NULL DEFAULT 'push',
  -- Directories are recorded too. Without them an empty directory leaves no trace at
  -- all, so a directory that genuinely exists on the device is indistinguishable from
  -- one that was never sent -- and the UI shows "absent" for something that is there.
  is_dir     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (device_id, path)
);
CREATE TABLE IF NOT EXISTS scans (
  device_id  TEXT PRIMARY KEY,
  scanned_at REAL,
  files      INTEGER,
  bytes      INTEGER
);
"""


@dataclass(frozen=True)
class DeviceState:
    device_id: str
    presence: Presence
    detail: str | None = None
    #: When and how this claim was recorded: PRESENT ON is a cache, so each slot says.
    at: float | None = None
    source: str = "push"

    @property
    def map_class(self) -> str:
        """The presence map's slot class: four states, four classes.

        The badges this replaced shared one plain class between `partial` and `absent`, so
        a directory a device holds 2 of 900 files of looked exactly like one it holds in
        full. A slot 4px wide has no tooltip to fall back on.
        """
        return {
            "ok": "p-ok",
            "stale": "p-stale",
            "partial": "p-part",
            "absent": "p-none",
        }[self.presence]

    @property
    def verb(self) -> str:
        return {"push": "pushed", "pull": "pulled"}.get(self.source, "seen in scan")


@dataclass(frozen=True)
class ManifestRow:
    device_id: str
    path: str
    blob: str | None
    size: int | None
    mtime: int | None
    shipped_at: float | None
    source: str


@dataclass(frozen=True)
class Extras:
    """What a device holds that the library does not -- and whether we know at all: a
    device never listed yields an empty difference too, and BK502 was once told it held
    "exactly what the library has" while carrying 36 files it did not."""

    rows: list[dict]
    total: int
    duplicates: int
    listed_bytes: int
    #: When the listing this is derived from was taken. None: never listed.
    scanned_at: float | None

    @property
    def known(self) -> bool:
        return self.scanned_at is not None

    @classmethod
    def unknown(cls) -> "Extras":
        return cls(rows=[], total=0, duplicates=0, listed_bytes=0, scanned_at=None)


def presence_slots(
    presence: dict[str, list[DeviceState]], device_ids: Sequence[str]
) -> dict[str, list[DeviceState | None]]:
    """One slot per device, in the order given, `None` where nothing is known.

    `presence` returns only where there is evidence, so its lists vary in length; drawn
    as they are, every slot after a gap shifts left and the map puts a book on the wrong
    device, with nothing failing. The row and its dialog both go through here.
    """
    return {
        path: [{s.device_id: s for s in states}.get(device_id) for device_id in device_ids]
        for path, states in presence.items()
    }


# --- the coverage map ------------------------------------------------------


@dataclass(frozen=True)
class Tally:
    """What one device holds of one folder, counted against the index (`coverage`)."""

    files: int = 0
    #: Their size as the index has it: a mirror's scan row carries the link's own size.
    bytes: int = 0
    #: Of `files`, copies whose content is not the library's (`_compare`'s rule).
    stale: int = 0
    pushed_at: float | None = None
    pulled_at: float | None = None

    def __add__(self, other: "Tally") -> "Tally":
        return Tally(
            self.files + other.files,
            self.bytes + other.bytes,
            self.stale + other.stale,
            _newest(self.pushed_at, other.pushed_at),
            _newest(self.pulled_at, other.pulled_at),
        )


def _newest(a: float | None, b: float | None) -> float | None:
    return b if a is None else a if b is None else max(a, b)


def _fill(held: int, total: int, floor: float, ceiling: float) -> float:
    """The drawn share, in percent: `floor` so something held never reads as empty,
    `ceiling` so something missing never reads as full. The exact share is in words."""
    if held <= 0 or total <= 0:
        return 0.0
    if held >= total:
        return 100.0
    return min(max(100.0 * held / total, floor), ceiling)


@dataclass(frozen=True)
class _Drawn:
    """A held/total pair and how it is painted: green for current copies, amber for
    stale ones, the track for the rest, and a hatch for what the device's excludes hold
    back (`excluded_roots`).

    `total` stays the library's count, so "minus excludes" can never read as the whole
    library: the share is measured against `total - excluded`, and the hatch keeps a
    device holding its whole share from drawing solid green end to end.
    """

    held: int
    total: int
    stale: int
    #: A scan has listed the device, so an empty answer is evidence, not a gap.
    scanned: bool
    #: Of `total`, the files the device's excludes hold back.
    excluded: int = 0
    #: Of `held`, the copies inside those: left there from before an exclude, which no
    #: push touches and no prune removes.
    held_out: int = 0

    FLOOR = 2.0
    CEILING = 97.0

    @property
    def share(self) -> int:
        """What the device's excludes let it take."""
        return self.total - self.excluded

    @property
    def held_in(self) -> int:
        return self.held - self.held_out

    @property
    def state(self) -> Coverage:
        if self.held and self.held >= self.total:
            return "complete"
        if self.excluded:
            if self.share <= 0:
                return "excluded"
            if self.held_in >= self.share:
                return "share"
        if self.held:
            return "partial"
        return "absent" if self.scanned else "unknown"

    @property
    def fill_ok(self) -> float:
        return self._fill_held() - self.fill_stale

    @property
    def fill_stale(self) -> float:
        if not self.stale:
            return 0.0
        held = self._fill_held()
        return min(max(held * self.stale / self.held, self.FLOOR), held)

    @property
    def fill_excluded(self) -> float:
        """The hatch, drawn last: none when the device holds even what it excludes."""
        if not self.excluded or self.state == "complete":
            return 0.0
        if self.share <= 0:
            return 100.0
        return _fill(self.excluded, self.total, self.FLOOR, self.CEILING)

    def _fill_held(self) -> float:
        state = self.state
        if state == "complete":
            return 100.0
        if state == "share":
            return 100.0 - self.fill_excluded
        if state == "excluded":
            # Only leftovers, drawn over the hatch.
            return _fill(self.held, self.total, self.FLOOR, self.CEILING)
        return min(
            _fill(self.held_in, self.total, self.FLOOR, self.CEILING),
            100.0 - self.fill_excluded,
        )


@dataclass(frozen=True)
class CoverageCell(_Drawn):
    """One device, one child folder: a square whose height is the share held."""

    name: str = ""

    # 14px tall: a fifth is the least that reads as "some", four fifths the most that does
    # not read as "all".
    FLOOR = 20.0
    CEILING = 80.0


@dataclass(frozen=True)
class CoverageRow(_Drawn):
    """One fleet device's line on the map: its bar, and a cell per column."""

    device: "Device | None" = None
    tally: Tally = field(default_factory=Tally)
    scanned_at: float | None = None
    cells: list[CoverageCell] = field(default_factory=list)
    #: The excluded roots below the entry, relative to it, and their size; empty when
    #: the entry is held back whole (`state` says so) or nothing is.
    excluded_names: list[str] = field(default_factory=list)
    excluded_bytes: int = 0


@dataclass(frozen=True)
class CoverageView:
    entry: Entry
    rows: list[CoverageRow]
    #: The child folders drawn as cells, in the table's order; empty past MAX_COLUMNS.
    columns: list[Entry]
    #: How many child folders there were when there were too many to draw; else 0.
    too_many: int = 0

    @property
    def is_tree(self) -> bool:
        """A directory with files is counted; a file or an empty directory is a yes/no."""
        return self.entry.is_dir and bool(self.entry.files)

    def count(self, state: Coverage) -> int:
        return sum(1 for r in self.rows if r.state == state)

    @property
    def held(self) -> int:
        return sum(1 for r in self.rows if r.held)

    @property
    def any_excluded(self) -> bool:
        return any(r.excluded for r in self.rows)


def coverage_view(
    entry: Entry,
    columns: Sequence[Entry],
    fleet: Sequence["Device"],
    tallies: dict[str, dict[str, Tally]],
    scanned: dict[str, float],
    too_many: int = 0,
    excluded: dict[str, list[Entry]] | None = None,
    leftovers: dict[str, dict[str, Tally]] | None = None,
) -> CoverageView:
    """The map of a directory with files: one row per fleet device, in fleet order, each
    summing its `coverage` tallies and drawing one cell per column.

    `excluded` is each device's `excluded_roots` that touch `entry`, and `leftovers`
    what it holds of each root below `entry` (`Manifests.held_under`)."""
    total = entry.files or 0
    excluded = excluded or {}
    leftovers = leftovers or {}
    rows = []
    for device in fleet:
        by_child = tallies.get(device.id, {})
        tally = sum(by_child.values(), Tally())
        was_scanned = device.id in scanned
        roots = excluded.get(device.id, [])
        left = leftovers.get(device.id, {})
        cells = []
        for col in columns:
            t = by_child.get(col.name, Tally())
            cells.append(
                CoverageCell(
                    held=t.files,
                    total=col.files or 0,
                    stale=t.stale,
                    scanned=was_scanned,
                    excluded=held_back(col, roots)[0],
                    held_out=_held_out(col, roots, left, t.files),
                    name=col.name,
                )
            )
        out_files, out_bytes = held_back(entry, roots)
        # Shallow first, so `names` keeps the top-level ones when it has to cut.
        inside = sorted(
            (r for r in roots if within(r.path, entry.path) and r.path != entry.path),
            key=lambda r: (r.path.count("/"), r.path),
        )
        rows.append(
            CoverageRow(
                held=tally.files,
                total=total,
                stale=tally.stale,
                scanned=was_scanned,
                excluded=out_files,
                held_out=_held_out(entry, roots, left, tally.files),
                device=device,
                tally=tally,
                scanned_at=scanned.get(device.id),
                cells=cells,
                excluded_names=[_relative(r.path, entry.path) for r in inside],
                excluded_bytes=out_bytes if inside else 0,
            )
        )
    return CoverageView(entry, rows, list(columns), too_many)


def _held_out(
    entry: Entry, roots: Sequence[Entry], leftovers: dict[str, Tally], held: int
) -> int:
    """Of the `held` copies under `entry`, those inside an excluded root: all of them when
    `entry` is itself inside one, else the sum of `leftovers` for the roots below it."""
    if any(within(entry.path, r.path) for r in roots):
        return held
    return sum(
        leftovers[r.path].files
        for r in roots
        if r.path in leftovers and within(r.path, entry.path)
    )


def _relative(path: str, base: str) -> str:
    return path[len(base) + 1 :] if base else path


def item_view(
    entry: Entry,
    fleet: Sequence["Device"],
    slots: Sequence[DeviceState | None],
    scanned: dict[str, float],
    excluded: dict[str, list[Entry]] | None = None,
) -> CoverageView:
    """The map of a file, or of an empty directory: `presence`'s yes/no per device, drawn
    with the same rows. `slots` comes from `presence_slots`, one per fleet device;
    `excluded` as for `coverage_view`."""
    excluded = excluded or {}
    rows = []
    for device, s in zip(fleet, slots, strict=True):
        held = s is not None and s.presence != "absent"
        at = s.at if s is not None else None
        source = s.source if s is not None else None
        out = any(within(entry.path, r.path) for r in excluded.get(device.id, []))
        rows.append(
            CoverageRow(
                held=int(held),
                total=1,
                stale=int(s is not None and s.presence == "stale"),
                scanned=device.id in scanned,
                excluded=int(out),
                held_out=int(out and held),
                device=device,
                tally=Tally(
                    files=int(held),
                    bytes=entry.size if held else 0,
                    pushed_at=at if source == "push" else None,
                    pulled_at=at if source == "pull" else None,
                ),
                scanned_at=scanned.get(device.id),
            )
        )
    return CoverageView(entry, rows, [])


class Manifests:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        #: `last_sync` per device. Every write below keeps it exact, which is safe because
        #: this object is the manifest's only writer. See `last_sync`.
        self._last_sync: dict[str, float | None] = {}
        #: Bumped after every write to a device, for `coverage`'s root cache. See there.
        self._writes: dict[str, int] = {}
        self._root: dict[str, tuple[int, str | None, dict[str, Tally]]] = {}
        self._ensure()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            existing = {r[1] for r in conn.execute("PRAGMA table_info(manifest)")}
            if "is_dir" not in existing:
                with conn:
                    conn.execute(
                        "ALTER TABLE manifest ADD COLUMN is_dir INTEGER NOT NULL DEFAULT 0"
                    )
            # No secondary index: the primary key (device_id, path) answers every query
            # here, `presence`'s batched IN included (3.65 ms with ix_manifest_path, 3.46
            # without, measured), and the two old indexes cost every write (20k scan rows,
            # 66 -> 47 ms) and 39 MiB. Dropped here because SCHEMA's IF NOT EXISTS would
            # leave them for ever; no VACUUM, which would lock the database while serving.
            with conn:
                conn.execute("DROP INDEX IF EXISTS ix_manifest_device")
                conn.execute("DROP INDEX IF EXISTS ix_manifest_path")
        finally:
            conn.close()

    # --- writing ---------------------------------------------------------

    def record(
        self,
        device_id: str,
        entries: Iterable[tuple],
        source: str = "push",
    ) -> int:
        """Upsert `(path, blob, size, mtime[, is_dir])` tuples for one device."""
        rows = _rows(device_id, entries, source)
        if not rows:
            return 0
        conn = self._connect()
        try:
            with conn:
                conn.executemany(_UPSERT, rows)
        finally:
            conn.close()
        self._stamped(device_id, rows)
        self._touched(device_id)
        return len(rows)

    def _touched(self, device_id: str) -> None:
        """After a write has committed, never before: see `coverage`."""
        self._writes[device_id] = self._writes.get(device_id, 0) + 1

    def _stamped(self, device_id: str, rows: list[tuple]) -> None:
        """Keep `last_sync` exact after an upsert: every row just written carries `now`."""
        if any(not r[7] for r in rows):
            self._last_sync[device_id] = rows[0][5]

    def record_entries(
        self, device_id: str, entries: Iterable[Entry], source: str = "push"
    ) -> int:
        return self.record(
            device_id,
            (
                (e.path, e.blob, e.size, e.mtime, int(e.is_dir))
                for e in entries
            ),
            source=source,
        )

    def replace_scan(
        self,
        device_id: str,
        entries: Iterable[tuple[str, str | None, int | None, int | None]],
        started_at: float | None = None,
    ) -> int:
        """A scan is authoritative: it replaces every row the device had before the listing
        began, whatever wrote it. Push rows once survived, and one, emptied by hand, kept
        30 pushed books through a scan that listed none (2026-10-02).

        A row written after `started_at` survives: a push that finished mid-scan may have
        landed in a directory the listing had already passed. None means it began now.
        """
        rows = _rows(device_id, entries, "scan")
        files = [r for r in rows if not r[7]]
        total_bytes = sum((r[3] or 0) for r in files)
        before = time.time() if started_at is None else started_at
        # One transaction. As three, a reader between the DELETE and the INSERT saw the
        # device holding nothing, and a crash there left it that way.
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM manifest WHERE device_id = ? "
                    "AND (shipped_at IS NULL OR shipped_at < ?)",
                    (device_id, before),
                )
                conn.executemany(_UPSERT, rows)
                conn.execute(
                    "INSERT INTO scans (device_id, scanned_at, files, bytes) "
                    "VALUES (?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET "
                    "  scanned_at=excluded.scanned_at, files=excluded.files, "
                    "  bytes=excluded.bytes",
                    (device_id, time.time(), len(files), total_bytes),
                )
        finally:
            conn.close()
        # The DELETE may have taken the newest row with it, so ask again next time.
        self._last_sync.pop(device_id, None)
        self._touched(device_id)
        return len(rows)

    def retract(self, device_id: str, paths: Iterable[str]) -> int:
        """Drop this device's rows for paths a transfer just deleted; returns how many.

        Not merely untidy if left: `presence` counts a directory's rows as a range, so a
        stale one adds to a numerator whose denominator just lost it -- `14 of 13`. Any
        source, since the deletion is the evidence at either end.
        """
        rows = [(device_id, path) for path in paths]
        if not rows:
            return 0
        self._last_sync.pop(device_id, None)
        conn = self._connect()
        try:
            with conn:
                cur = conn.executemany(
                    "DELETE FROM manifest WHERE device_id = ? AND path = ?", rows
                )
                removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        finally:
            conn.close()
        self._touched(device_id)
        return removed

    def forget(self, device_id: str) -> None:
        self._last_sync.pop(device_id, None)
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM manifest WHERE device_id = ?", (device_id,))
                conn.execute("DELETE FROM scans WHERE device_id = ?", (device_id,))
        finally:
            conn.close()
        self._touched(device_id)

    # --- reading ---------------------------------------------------------

    def summary(self, device_id: str) -> tuple[int, int, float | None]:
        conn = self._connect()
        try:
            # Files only: counting directories would inflate "20,782 files" to 24,621.
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size),0), MAX(shipped_at) "
                "FROM manifest WHERE device_id = ? AND is_dir = 0",
                (device_id,),
            ).fetchone()
        finally:
            conn.close()
        return (row[0], row[1], row[2])

    def last_sync(self, device_id: str) -> float | None:
        """The newest file row's `shipped_at`, or None.

        Cached, because the Devices page asks for every device on every 10 s poll -- twice,
        rows and status -- and the aggregate reads the device's whole manifest slice: 159 ms
        per render over 534,759 rows, measured on a copy of the live database, all of it
        blocking the event loop the SSE dock shares. Only a write can change the answer, and
        this object makes every write, so each one updates or drops the entry.
        """
        if device_id not in self._last_sync:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT MAX(shipped_at) FROM manifest WHERE device_id = ? AND is_dir = 0",
                    (device_id,),
                ).fetchone()
            finally:
                conn.close()
            self._last_sync[device_id] = row[0] if row else None
        return self._last_sync[device_id]

    def scanned_at(self, device_id: str) -> float | None:
        """When this device was last listed, or None if it never has been.

        The `scans` row is written only by a completed `replace_scan`, so this is the one
        honest answer to "has anyone ever asked the device what it holds".
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT scanned_at FROM scans WHERE device_id = ?", (device_id,)
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def scanned_all(self, device_ids: Sequence[str]) -> dict[str, float]:
        """`scanned_at` for several devices at once; a device never listed is missing."""
        if not device_ids:
            return {}
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT device_id, scanned_at FROM scans WHERE device_id IN "
                f"({','.join('?' * len(device_ids))})",
                list(device_ids),
            ).fetchall()
        finally:
            conn.close()
        return {r[0]: r[1] for r in rows if r[1] is not None}

    def coverage(
        self, index_db: Path, prefix: str, device_ids: Sequence[str]
    ) -> dict[str, dict[str, Tally]]:
        """How much of the directory `prefix` each device holds, per child: the first path
        segment below it, `""` for its loose files.

        Joined to the index on `path`, so only the library's own files count. A range count
        cannot say that: dragon has 85,248 file rows for 20,794 library files (the vault,
        urantia-library, `.sdr/`), nexus10 327 rows under the categories for 105 books. The
        sizes come from the index as well.

        Priced by the subtree -- Science 101 ms for the fleet, Fiction 34 -- except the
        root, which is every device's whole slice: 450-650 ms, measured on pi5 against the
        live databases. So the root alone is cached, per device, keyed on the index's
        `indexed_at` and on `_writes`, which every write bumps after committing: a result
        computed across a write carries the older count, and is never served. Blocking;
        the route runs it on a thread.
        """
        out: dict[str, dict[str, Tally]] = {d: {} for d in device_ids}
        if not device_ids or not index_db.exists():
            return out
        conn = sqlite3.connect(f"file:{self.db_path}", uri=True, timeout=10.0)
        try:
            conn.execute("ATTACH DATABASE ? AS idx", (f"file:{index_db}?mode=ro",))
            row = conn.execute("SELECT v FROM idx.meta WHERE k = 'indexed_at'").fetchone()
            generation = row[0] if row else None
            for device_id in device_ids:
                if prefix:
                    out[device_id] = self._tally(conn, device_id, prefix)
                    continue
                writes = self._writes.get(device_id, 0)
                hit = self._root.get(device_id)
                if hit and hit[0] == writes and hit[1] == generation:
                    out[device_id] = hit[2]
                    continue
                out[device_id] = self._tally(conn, device_id, prefix)
                self._root[device_id] = (writes, generation, out[device_id])
        finally:
            conn.close()
        return out

    def held_under(
        self, index_db: Path, roots: dict[str, list[Entry]]
    ) -> dict[str, dict[str, Tally]]:
        """What each device holds under each of its excluded roots, counted as `coverage`
        counts: the leftovers `coverage_view` keeps out of the share. Priced by the roots'
        subtrees. Blocking; the route runs it on a thread."""
        out: dict[str, dict[str, Tally]] = {d: {} for d in roots}
        if not any(roots.values()) or not index_db.exists():
            return out
        conn = sqlite3.connect(f"file:{self.db_path}", uri=True, timeout=10.0)
        try:
            conn.execute("ATTACH DATABASE ? AS idx", (f"file:{index_db}?mode=ro",))
            for device_id, entries in roots.items():
                for root in entries:
                    if root.is_dir:
                        tally = sum(self._tally(conn, device_id, root.path).values(), Tally())
                    else:
                        (n,) = conn.execute(
                            "SELECT COUNT(*) FROM manifest "
                            "WHERE device_id = ? AND path = ? AND is_dir = 0",
                            (device_id, root.path),
                        ).fetchone()
                        tally = Tally(files=n)
                    out[device_id][root.path] = tally
        finally:
            conn.close()
        return out

    @staticmethod
    def _tally(conn: sqlite3.Connection, device_id: str, prefix: str) -> dict[str, Tally]:
        rest = "substr(m.path, :start)"
        child = (
            f"CASE WHEN instr({rest}, '/') = 0 THEN '' "
            f"ELSE substr({rest}, 1, instr({rest}, '/') - 1) END"
        )
        # The half-open range of `presence`, for the reason given there; the root is the
        # whole slice, and the join drops what is not the library's.
        where = "AND m.path >= :lo AND m.path < :hi" if prefix else ""
        rows = conn.execute(
            f"SELECT {child}, COUNT(*), COALESCE(SUM(e.size), 0), "
            # `_compare`, as SQL: by blob where both have one, else by size.
            "  SUM(CASE WHEN e.blob IS NOT NULL AND m.blob IS NOT NULL THEN e.blob != m.blob"
            "           WHEN m.size IS NOT NULL THEN m.size != e.size ELSE 0 END), "
            "  MAX(CASE WHEN m.source = 'push' THEN m.shipped_at END), "
            "  MAX(CASE WHEN m.source = 'pull' THEN m.shipped_at END) "
            "FROM manifest m JOIN idx.entries e ON e.path = m.path AND e.is_dir = 0 "
            f"WHERE m.device_id = :device AND m.is_dir = 0 {where} "
            "GROUP BY 1",
            {
                "device": device_id,
                "start": len(prefix) + 2 if prefix else 1,
                "lo": f"{prefix}/",
                "hi": f"{prefix}0",
            },
        ).fetchall()
        return {r[0]: Tally(r[1], r[2], r[3] or 0, r[4], r[5]) for r in rows}

    def presence(
        self, entries: Sequence[Entry], device_ids: Sequence[str]
    ) -> dict[str, list[DeviceState]]:
        """Per-row `PRESENT ON` state for a page of the file table: one batched query for
        the files, then one range count per directory per device -- priced by the subtree,
        never by the device's whole manifest."""
        if not entries or not device_ids:
            return {}

        files = [e for e in entries if not e.is_dir]
        dirs = [e for e in entries if e.is_dir]
        out: dict[str, list[DeviceState]] = {e.path: [] for e in entries}

        conn = self._connect()
        try:
            if files:
                by_path = {e.path: e for e in files}
                placeholders = ",".join("?" * len(by_path))
                dev_ph = ",".join("?" * len(device_ids))
                rows = conn.execute(
                    f"SELECT device_id, path, blob, size, mtime, shipped_at, source "
                    f"FROM manifest "
                    f"WHERE path IN ({placeholders}) AND device_id IN ({dev_ph})",
                    [*by_path.keys(), *device_ids],
                ).fetchall()
                found: dict[tuple[str, str], sqlite3.Row] = {
                    (r["device_id"], r["path"]): r for r in rows
                }
                for entry in files:
                    for device_id in device_ids:
                        row = found.get((device_id, entry.path))
                        if row is None:
                            continue
                        out[entry.path].append(
                            DeviceState(
                                device_id,
                                _compare(entry, row),
                                at=row["shipped_at"],
                                source=row["source"],
                            )
                        )

            for entry in dirs:
                # A half-open range on the primary key, never `path LIKE 'dir/%'`: SQLite
                # serves that LIKE from no index (ESCAPE and case_sensitive_like=OFF each
                # disable it), so every count scanned the device's whole slice. One page of
                # /Books/Fiction, 304 dirs x 15 devices: 18.10 s with LIKE, 30 ms with this.
                # The bound bumps the trailing "/" to "0", its exact BINARY successor, safe
                # where a U+FFFF sentinel drops non-BMP names. Verified equal over the whole
                # live index, except that LIKE was case-insensitive and wrong.
                lo = f"{entry.path}/"
                hi = f"{entry.path}0"
                total = entry.files or 0
                for device_id in device_ids:
                    row = conn.execute(
                        "SELECT COUNT(*), MAX(shipped_at), MAX(source) FROM manifest "
                        "WHERE device_id = ? AND path >= ? AND path < ? "
                        "  AND is_dir = 0",
                        (device_id, lo, hi),
                    ).fetchone()
                    have = row[0] if row else 0
                    seen_at = row[1] if row else None
                    seen_src = (row[2] if row else None) or "push"

                    if total == 0:
                        # An empty directory's only evidence is its own row.
                        exists = conn.execute(
                            "SELECT shipped_at, source FROM manifest "
                            "WHERE device_id = ? AND path = ? AND is_dir = 1",
                            (device_id, entry.path),
                        ).fetchone()
                        if exists:
                            out[entry.path].append(
                                DeviceState(
                                    device_id,
                                    "ok",
                                    "empty",
                                    at=exists[0],
                                    source=exists[1] or "push",
                                )
                            )
                        continue

                    if not have:
                        continue
                    state: Presence = "ok" if have >= total else "partial"
                    out[entry.path].append(
                        DeviceState(
                            device_id,
                            state,
                            f"{have}/{total}",
                            at=seen_at,
                            source=seen_src,
                        )
                    )
        finally:
            conn.close()
        return out

    def paths_for(self, device_id: str) -> set[str]:
        """Every file path the device is known to hold."""
        conn = self._connect()
        try:
            return {
                r[0]
                for r in conn.execute(
                    "SELECT path FROM manifest WHERE device_id = ? AND is_dir = 0",
                    (device_id,),
                )
            }
        finally:
            conn.close()

    def extras(
        self,
        device_id: str,
        library_paths: set[str],
        limit: int = 500,
        expected_toplevel: frozenset[str] = frozenset(),
    ) -> Extras:
        """Files on the device that the library does not have: retired books, and copies
        under mangled names (each row says whether its decoded name is in the library).

        Scan rows only: push and pull rows can never name a file we lack, so note10's
        20,782 push rows once reported a clean bill of health nothing had checked.
        `expected_toplevel` (SKIP_TOPLEVEL, for a CAS node) keeps a correct vault from being
        listed as ~24.6k orphans. A CAS node's book is a link with size 0, so its size comes
        through the vault row the same scan listed; unresolvable is None, drawn as a dash.
        """
        from .scan import demangle

        scanned = self.scanned_at(device_id)
        if scanned is None:
            return Extras.unknown()

        conn = self._connect()
        try:
            scanned_rows = conn.execute(
                "SELECT path, size, blob FROM manifest "
                "WHERE device_id = ? AND is_dir = 0 AND source = 'scan'",
                (device_id,),
            ).fetchall()
        finally:
            conn.close()

        sizes = {r[0]: r[1] for r in scanned_rows}
        blobs = {r[0]: r[2] for r in scanned_rows}
        # By the vault row's basename, so a sharded vault would still match.
        by_blob = {
            r[0].rsplit("/", 1)[-1]: r[1]
            for r in scanned_rows
            if r[1] and "/" in r[0]
        }

        found = sorted(set(sizes) - library_paths)
        if expected_toplevel:
            found = [
                p for p in found if p.split("/", 1)[0] not in expected_toplevel
            ]
        rows: list[dict] = []
        duplicates = 0
        for path in found:
            real = demangle(path)
            is_dup = bool(real and real in library_paths)
            if is_dup:
                duplicates += 1
            if len(rows) < limit:
                size = sizes.get(path) or 0
                if not size:
                    blob = blobs.get(path)
                    size = by_blob.get(blob) if blob else None
                rows.append(
                    {
                        "path": path,
                        "size": size,
                        "real": real,
                        "duplicate": is_dup,
                    }
                )
        return Extras(
            rows=rows,
            total=len(found),
            duplicates=duplicates,
            # What is listed, beside "showing first 500 of N".
            listed_bytes=sum(r["size"] or 0 for r in rows),
            scanned_at=scanned,
        )

    def rows_for(self, device_id: str, limit: int = 5000) -> list[ManifestRow]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT device_id, path, blob, size, mtime, shipped_at, source "
                "FROM manifest WHERE device_id = ? ORDER BY path LIMIT ?",
                (device_id, limit),
            ).fetchall()
        finally:
            conn.close()
        return [ManifestRow(*tuple(r)) for r in rows]


_UPSERT = (
    "INSERT INTO manifest "
    "(device_id, path, blob, size, mtime, shipped_at, source, is_dir) "
    "VALUES (?,?,?,?,?,?,?,?) "
    "ON CONFLICT(device_id, path) DO UPDATE SET "
    "  blob=excluded.blob, size=excluded.size, mtime=excluded.mtime, "
    "  shipped_at=excluded.shipped_at, source=excluded.source, is_dir=excluded.is_dir"
)


def _rows(device_id: str, entries: Iterable[tuple], source: str) -> list[tuple]:
    """`(path, blob, size, mtime[, is_dir])` tuples as `_UPSERT` parameters."""
    now = time.time()
    rows = []
    for item in entries:
        path, blob, size, mtime = item[:4]
        is_dir = int(item[4]) if len(item) > 4 else 0
        rows.append((device_id, path, blob, size, mtime, now, source, is_dir))
    return rows


def _compare(entry: Entry, row: sqlite3.Row) -> Presence:
    """Does the device's copy still match? By blob hash where the row has one (exact),
    else by size. Never by mtime, which marked a correct 249 GB library entirely stale."""
    if entry.blob and row["blob"]:
        return "ok" if entry.blob == row["blob"] else "stale"
    if row["size"] is not None and row["size"] != entry.size:
        return "stale"
    return "ok"


__all__ = [
    "MAX_COLUMNS",
    "CoverageCell",
    "CoverageRow",
    "CoverageView",
    "DeviceState",
    "Extras",
    "ManifestRow",
    "Manifests",
    "Presence",
    "Tally",
    "coverage_view",
    "item_view",
]
