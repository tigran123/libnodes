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
CREATE INDEX IF NOT EXISTS ix_manifest_path ON manifest(path);
CREATE INDEX IF NOT EXISTS ix_manifest_device ON manifest(device_id);
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
        return "pushed" if self.source == "push" else "seen in scan"


@dataclass(frozen=True)
class ManifestRow:
    device_id: str
    path: str
    blob: str | None
    size: int | None
    mtime: int | None
    shipped_at: float | None
    source: str


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
        """A scan is authoritative: it replaces every scan-sourced row for the device."""
        rows = list(entries)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM manifest WHERE device_id = ? AND source = 'scan'",
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

    def presence(
        self, entries: Sequence[Entry], device_ids: Sequence[str]
    ) -> dict[str, list[DeviceState]]:
        """Per-row `PRESENT ON` state for a page of the file table.

        Two queries total regardless of row count: one exact match for files, one
        prefix aggregate for directories.
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
                prefix = f"{entry.path}/"
                total = entry.files or 0
                for device_id in device_ids:
                    row = conn.execute(
                        "SELECT COUNT(*), MAX(shipped_at), MAX(source) FROM manifest "
                        "WHERE device_id = ? AND path LIKE ? ESCAPE '\\' "
                        "  AND is_dir = 0",
                        (device_id, _escape_like(prefix) + "%"),
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

    def extras(self, device_id: str, library_paths: set[str], limit: int = 500):
        """Files on the device that the library does not have.

        Orphans accumulate: books deleted from the library, and — as found on a real
        device — copies whose names were mangled by whatever wrote them, so the same
        book sits there twice under two encodings of one filename.

        Returns ``(rows, total, duplicates)`` where each row carries the decoded name
        when one can be recovered, and whether that name is in the library — which is
        what makes it safe to delete.
        """
        from .scan import demangle

        conn = self._connect()
        try:
            sizes = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT path, size FROM manifest "
                    "WHERE device_id = ? AND is_dir = 0",
                    (device_id,),
                )
            }
        finally:
            conn.close()

        found = sorted(set(sizes) - library_paths)
        rows = []
        duplicates = 0
        for path in found:
            real = demangle(path)
            is_dup = bool(real and real in library_paths)
            if is_dup:
                duplicates += 1
            if len(rows) < limit:
                rows.append(
                    {
                        "path": path,
                        "size": sizes.get(path) or 0,
                        "real": real,
                        "duplicate": is_dup,
                    }
                )
        return rows, len(found), duplicates

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


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = ["DeviceState", "ManifestRow", "Manifests", "Presence"]
