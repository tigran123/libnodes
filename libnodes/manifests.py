"""What is actually on each device.

Two things write here: a completed push (we know exactly what rsync shipped) and an
optional remote scan (we ask the device what it holds). Both land in the same table with
a `source` column, so `PRESENT ON` can distinguish "we sent this" from "we saw this".

Staleness is decided on the blob hash where we have one. Because the library is
content-addressed, a hash mismatch means the file genuinely changed — no false positives
from an rsync that rewrote mtimes, and no false negatives from an edit that happened to
preserve size.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Sequence

from .library import Entry

Presence = Literal["ok", "stale", "partial", "absent"]

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
    #: When this claim was recorded, and how. PRESENT ON is a cache, so every chip
    #: carries its own provenance rather than the view carrying one device-wide figure
    #: that says nothing about the row you are actually looking at.
    at: float | None = None
    source: str = "push"

    @property
    def badge_class(self) -> str:
        return {
            "ok": "badge-ok",
            "stale": "badge-warn",
            "partial": "badge",
            "absent": "badge",
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
    """What a device holds that the library does not — and whether we know at all.

    `scanned_at is None` is the state the old three-tuple could not express. A device
    nobody has ever listed produces an empty set difference, exactly like a device that
    genuinely holds nothing extra, and the dialog rendered that emptiness as a claim:
    BK502 was told it "holds exactly what the library has, and no more" while carrying 36
    files, none of them from the library and not one of them ever recorded here.
    """

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


class Manifests:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
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
            # Both of the old secondary indexes are gone, and neither was ever read.
            #
            # ix_manifest_device was wholly redundant: PRIMARY KEY (device_id, path) is
            # itself an index whose first column is device_id, so it answers every
            # `device_id = ?` lookup here -- nine of the ten statements in this module --
            # and answers them better, because it carries `path` and so needs no table
            # lookup per row (`browse` becomes a COVERING INDEX scan).
            #
            # ix_manifest_path was read by nothing at all. The tenth statement is the only
            # one that does not lead with `device_id`, `presence`'s batched
            # `path IN (...) AND device_id IN (...)`, and SQLite answers that from the
            # primary key too: measured on a copy of the live database, 300 paths x 15
            # devices planned identically and ran in 3.65 ms with the index and 3.46 ms
            # without it. What it did cost was every write -- 20,000 recorded scan rows
            # went 66 ms to 47 ms with it gone -- and 39 MiB of a 117 MiB file.
            #
            # Dropped here rather than simply removed from SCHEMA, because every statement
            # there is IF NOT EXISTS and would leave the live ones in place for ever. The
            # pages are freed for reuse but the file does not shrink without a VACUUM,
            # which is deliberately not done here: it rewrites the whole database under an
            # exclusive lock while the fleet is being served.
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
        now = time.time()
        rows = []
        for item in entries:
            path, blob, size, mtime = item[:4]
            is_dir = int(item[4]) if len(item) > 4 else 0
            rows.append((device_id, path, blob, size, mtime, now, source, is_dir))
        if not rows:
            return 0
        conn = self._connect()
        try:
            with conn:
                conn.executemany(
                    "INSERT INTO manifest "
                    "(device_id, path, blob, size, mtime, shipped_at, source, is_dir) "
                    "VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(device_id, path) DO UPDATE SET "
                    "  blob=excluded.blob, size=excluded.size, mtime=excluded.mtime, "
                    "  shipped_at=excluded.shipped_at, source=excluded.source, "
                    "  is_dir=excluded.is_dir",
                    rows,
                )
        finally:
            conn.close()
        return len(rows)

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
        self, device_id: str, entries: Iterable[tuple[str, str | None, int | None, int | None]]
    ) -> int:
        """A scan is authoritative: it replaces every scan-sourced row for the device.

        And every pull-sourced one, which is the same rule and not a second one. A pull
        row says "this node sent us this file", which was true when it was written and
        can stop being true the moment somebody deletes the book upstream -- and nothing
        else would ever retract it, so it would read present for ever. A scan has just
        looked; it gets to overrule what a transfer once implied. Push rows survive, as
        they always have: they are a claim about a device we write to, which no scan of
        some other node contradicts.
        """
        rows = list(entries)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM manifest WHERE device_id = ? AND source IN ('scan', 'pull')",
                    (device_id,),
                )
        finally:
            conn.close()
        written = self.record(device_id, rows, source="scan")
        total_bytes = sum(
            (r[2] or 0) for r in rows if not (len(r) > 4 and r[4])
        )
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO scans (device_id, scanned_at, files, bytes) "
                    "VALUES (?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET "
                    "  scanned_at=excluded.scanned_at, files=excluded.files, "
                    "  bytes=excluded.bytes",
                    (device_id, time.time(), written, total_bytes),
                )
        finally:
            conn.close()
        return written

    def retract(self, device_id: str, paths: Iterable[str]) -> int:
        """Drop this device's rows for paths it no longer has. Returns how many went.

        The narrow counterpart to `replace_scan`'s wholesale retraction, for the one case
        that knows without looking: a transfer that just deleted the file. A pull prunes
        what the upstream removed, and the row saying that upstream still holds it is
        wrong the instant rsync prints `deleting`.

        Leaving it is not merely untidy. `presence` counts a directory's files as a
        half-open range over `(device_id, path)`, so a stale row is a file added to the
        numerator of a fraction whose denominator has just lost one -- `14 of 13` in
        PRESENT ON -- and `summary` carries the same row into the node's file and byte
        totals.

        Not restricted by source, although only `_debit_pull` calls it today: the evidence
        is the deletion, and a row's source does not change what that deletion proves. A
        mirror's outward `--delete` is the same argument pointed the other way and is not
        wired up yet — see TODO.md. Batched in one transaction, because a prune arrives as
        a list.
        """
        rows = [(device_id, path) for path in paths]
        if not rows:
            return 0
        conn = self._connect()
        try:
            with conn:
                cur = conn.executemany(
                    "DELETE FROM manifest WHERE device_id = ? AND path = ?", rows
                )
                return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        finally:
            conn.close()

    def forget(self, device_id: str) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM manifest WHERE device_id = ?", (device_id,))
                conn.execute("DELETE FROM scans WHERE device_id = ?", (device_id,))
        finally:
            conn.close()

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
        return self.summary(device_id)[2]

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

    def presence(
        self, entries: Sequence[Entry], device_ids: Sequence[str]
    ) -> dict[str, list[DeviceState]]:
        """Per-row `PRESENT ON` state for a page of the file table.

        One batched query for every file on the page, and then one per directory per
        device: a directory wants a COUNT over its own subtree, and those do not batch
        into the exact-match form. What each must never be is priced by the size of the
        *device's* manifest, so it is a half-open index range on the primary key
        `(device_id, path)` -- never a LIKE prefix, for the reason beside the query.

        This said "two queries total regardless of row count" for a year, which is
        exactly how a 4,560-query nested loop came to sit under it unremarked. Count
        them before believing a sentence like that one.
        """
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
                # A half-open range on the primary key, *not* `path LIKE 'dir/%'`.
                # SQLite will not serve that LIKE from an index, for two independent
                # reasons: the ESCAPE clause disables the LIKE optimisation outright, and
                # so does the default case_sensitive_like=OFF against a BINARY-collated
                # column. Every one of these therefore planned as `SEARCH manifest USING
                # INDEX ix_manifest_device (device_id=?)` -- a full scan of that device's
                # slice, priced by what the *device* holds rather than by the subtree
                # being asked about, so three files cost the same as three thousand.
                # One page of /Books/Fiction is 304 directories x 15 devices = 4,560 of
                # them against a 268,692-row manifest (dragon alone 91,032): 18.10 s
                # measured, against 30 ms for the range below. The root was 1.12 s. No
                # schema change bought it -- (device_id, path) was already the key.
                #
                # The upper bound is the prefix with its trailing "/" (0x2F) bumped to
                # "0" (0x30), the exact successor of the prefix under BINARY collation
                # and safe for every path -- unlike the U+FFFF sentinel this idiom is
                # usually written with, which silently drops any name starting with a
                # non-BMP character (F0... sorts above EF BF BF).
                #
                # Verified equivalent over the whole live index: 23,064 directory x
                # device comparisons, zero mismatches. The one deliberate difference is
                # that LIKE was case-INSENSITIVE, so `Fiction/Abramov` had been counting
                # the files under `Fiction/abramov/` as its own. The range is exact.
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
                        # An empty directory holds no files to count, so the only
                        # evidence is the directory row itself. Without this an empty
                        # directory that IS on the device reads as absent.
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
        """Files on the device that the library does not have.

        Orphans accumulate: books deleted from the library, and — as found on a real
        device — copies whose names were mangled by whatever wrote them, so the same
        book sits there twice under two encodings of one filename.

        **Only a scan can answer this.** The comparison runs over scan-sourced rows
        alone, because a push manifest records what LibNodes *sent*: by construction it
        cannot contain a file LibNodes did not send, so subtracting the library from it
        always yields nothing whatever the device holds. note10 carried 20,782 push rows
        and no scan, and reported a clean bill of health it had no way to have checked.
        A pull row is no better here for the mirror-image reason: it records what the
        node sent *us*, so it can never name a file we do not have.

        Each row carries the decoded name when one can be recovered, and whether that
        name is in the library — which is what makes it safe to delete.

        `expected_toplevel` is for a CAS-shaped device — a mirror or an upstream — and
        without it this answer inverts on one: such a node holds `.data/` and
        `urantia-library/`, neither of which is in the index, so every blob in the vault
        would be reported as an orphan — around 24,616 rows of "delete me" describing a
        correct replica. Those names are expected there, so they are excluded rather than
        listed. Pass `SKIP_TOPLEVEL`; a reader passes nothing and the comparison is
        unchanged.

        **Sizes on a CAS-shaped node come out of the vault, not off the row.** A scan of
        one records a book as a symlink with size 0 and a blake2b hash (`scan.parse_line`:
        the link's own 143 bytes would be a lie about the book, and a hash is the stronger
        claim). The same scan also lists the vault itself, so the real size is already in
        this table under `.data/<hash>` — resolve through it rather than printing `0 B`
        for a book. A zero is a claim; where nothing can be resolved the row says so with
        `size: None` and the dialog draws a dash.
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
        # Keyed off the vault row's own basename rather than assembling ".data/" + hash:
        # if the vault is ever sharded, an assumed path silently stops matching and every
        # book goes back to reading 0 B, while a basename lookup keeps working.
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
                        # None, not 0, when nothing could be resolved: a dash is an
                        # admission and a zero is a claim about the book.
                        "size": size,
                        "real": real,
                        "duplicate": is_dup,
                    }
                )
        return Extras(
            rows=rows,
            total=len(found),
            duplicates=duplicates,
            # The bytes of what is *listed*, not of everything found: the dialog prints
            # this beside "showing first 500 of N", where a total would not match.
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


def _compare(entry: Entry, row: sqlite3.Row) -> Presence:
    """Does the device's copy still match the library's?

    Two levels of confidence, and it matters which one we have:

    * **We pushed it** — the row carries the blob hash, so this is exact. In a
      content-addressed library a hash match *is* content identity.
    * **We scanned it** — a remote listing gives size and mtime, never content. Size is
      then the only honest signal.

    mtime is deliberately not used. The device's copies carry whatever time they were
    written, filesystems disagree about granularity (vfat rounds to 2s), and a
    content-addressed library changes a file's bytes by changing its hash, not by
    touching it in place. Comparing mtimes here marked a correct 249 GB library as
    entirely stale.
    """
    if entry.blob and row["blob"]:
        return "ok" if entry.blob == row["blob"] else "stale"
    if row["size"] is not None and row["size"] != entry.size:
        return "stale"
    return "ok"


__all__ = ["DeviceState", "Extras", "ManifestRow", "Manifests", "Presence"]
