"""The cached library index.

Every book is a symlink into `/Books/.data/<blake2b>`, so sizes come from following the
link, and the target's basename is a free exact content identity that the manifest and
the catalog join key off. The walk (1.0 s on pi5, ~29 s on the Pi 3) runs on one
background thread and publishes by atomic rename; readers open short read-only
connections and never see a half-built index.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .config import SKIP_TOPLEVEL, Settings

log = logging.getLogger(__name__)

_BLOB_RE = re.compile(r"^[0-9a-f]{32,128}$")

SCHEMA = """
CREATE TABLE entries (
  path   TEXT PRIMARY KEY,
  parent TEXT,
  name   TEXT NOT NULL,
  is_dir INTEGER NOT NULL,
  fmt    TEXT,
  size   INTEGER NOT NULL,
  mtime  INTEGER NOT NULL,
  files  INTEGER,
  blob   TEXT,
  title  TEXT,
  author TEXT
);
CREATE INDEX ix_entries_parent ON entries(parent);
CREATE INDEX ix_entries_name   ON entries(name COLLATE NOCASE);
CREATE INDEX ix_entries_blob   ON entries(blob);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
"""

SORTS = {
    "name": "is_dir DESC, name COLLATE NOCASE ASC",
    "size": "is_dir DESC, size DESC",
    "modified": "is_dir DESC, mtime DESC",
}

_COLUMNS = "path, parent, name, is_dir, fmt, size, mtime, files, blob, title, author"


@dataclass(frozen=True)
class Entry:
    path: str
    parent: str | None
    name: str
    is_dir: bool
    fmt: str | None
    size: int
    mtime: int
    files: int | None
    blob: str | None
    title: str | None
    author: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row | Sequence) -> "Entry":
        return cls(
            path=row[0],
            parent=row[1],
            name=row[2],
            is_dir=bool(row[3]),
            fmt=row[4],
            size=row[5],
            mtime=row[6],
            files=row[7],
            blob=row[8],
            title=row[9],
            author=row[10],
        )

    @property
    def label(self) -> str:
        return self.name + "/" if self.is_dir else self.name


@dataclass(frozen=True)
class IndexMeta:
    indexed_at: float | None
    entry_count: int
    file_count: int
    total_bytes: int
    duration: float | None
    errors: int
    running: bool
    #: Why the last rebuild failed, or None. The published index is still the previous
    #: good one, so without this a failing rebuild looked like an index that was merely old.
    error: str | None = None

    @property
    def ready(self) -> bool:
        return self.indexed_at is not None


# --------------------------------------------------------------------- guard --


class PathError(ValueError):
    """A requested path is not something we are willing to look at."""


def normalise(path: str | None) -> str:
    """Reduce a user-supplied `?p=` to a clean library-relative path.

    Rejects absolute paths and any traversal. This is the cheap first gate; the real
    authority is the index itself — see `LibraryIndex.require()`.
    """
    if not path:
        return ""
    raw = path.strip().strip("/")
    if not raw:
        return ""
    if raw.startswith("/") or ".." in raw.split("/"):
        raise PathError(f"illegal path: {path!r}")
    cleaned = os.path.normpath(raw)
    if cleaned in (".", "/"):
        return ""
    if cleaned.startswith("..") or cleaned.startswith("/"):
        raise PathError(f"illegal path: {path!r}")
    return cleaned


# --------------------------------------------------------------------- index --


class LibraryIndex:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = Path(settings.library_root)
        self.db_path = settings.index_db
        self._lock = threading.Lock()
        self._running = False
        self._last_error: str | None = None

    # --- reading ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection | None:
        if not self.db_path.exists():
            return None
        try:
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, timeout=5.0
            )
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error:
            return None

    def meta(self) -> IndexMeta:
        conn = self._connect()
        if conn is None:
            return IndexMeta(None, 0, 0, 0, None, 0, self._running, self._last_error)
        try:
            rows = dict(conn.execute("SELECT k, v FROM meta").fetchall())
        except sqlite3.Error:
            return IndexMeta(None, 0, 0, 0, None, 0, self._running, self._last_error)
        finally:
            conn.close()

        def num(key: str, cast=int, default=0):
            try:
                return cast(rows[key])
            except (KeyError, TypeError, ValueError):
                return default

        return IndexMeta(
            indexed_at=num("indexed_at", float, None) or None,
            entry_count=num("entry_count"),
            file_count=num("file_count"),
            total_bytes=num("total_bytes"),
            duration=num("duration", float, None),
            errors=num("errors"),
            running=self._running,
            error=self._last_error,
        )

    def entry(self, path: str) -> Entry | None:
        path = normalise(path)
        if path == "":
            return self._root_entry()
        conn = self._connect()
        if conn is None:
            return None
        try:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM entries WHERE path = ?", (path,)
            ).fetchone()
        finally:
            conn.close()
        return Entry.from_row(row) if row else None

    def _root_entry(self) -> Entry:
        m = self.meta()
        return Entry(
            path="",
            parent=None,
            name=str(self.root),
            is_dir=True,
            fmt=None,
            size=m.total_bytes,
            mtime=int(m.indexed_at or 0),
            files=m.file_count,
            blob=None,
            title=None,
            author=None,
        )

    def require(self, path: str | None) -> Entry:
        """Resolve `path` or raise. The index is the whitelist.

        Stronger than a `resolve().is_relative_to(root)` check, which the CAS symlinks
        would happily satisfy for anything inside `.data` that we never meant to expose.
        """
        clean = normalise(path)
        entry = self.entry(clean)
        if entry is None:
            raise PathError(f"not in index: {path!r}")
        return entry

    def children(
        self,
        path: str,
        *,
        q: str | None = None,
        sort: str = "name",
        limit: int = 2000,
    ) -> list[Entry]:
        """Rows for the file table: one directory's listing, which `q` narrows rather than
        turning into a search. The recursive search it once was scanned all 24.6k entries
        to answer with bare basenames, and could never return the directory being typed
        towards. `ix_entries_parent` keeps it one level however large the library grows.
        """
        conn = self._connect()
        if conn is None:
            return []
        where = ["parent IS ?"]
        params: list[object] = [path]

        if q:
            where.append("name LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(q)}%")

        order = SORTS.get(sort, SORTS["name"])
        sql = (
            f"SELECT {_COLUMNS} FROM entries WHERE {' AND '.join(where)} "
            f"ORDER BY {order} LIMIT ?"
        )
        params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return [Entry.from_row(r) for r in rows]

    def subtree(self, path: str) -> list[Entry]:
        """Every entry below `path`, not including it, in one query.

        A half-open range on the `path` primary key, the idiom `max_file_size` and
        `Manifests.presence` explain. It replaced a walk that asked `children()` once per
        directory: 3,846 connections and queries to record a Full Sync, 817 ms against
        42 ms measured on the live index, run on the event loop as each push finished --
        and capped at 20,000 children a directory, silently.
        """
        path = normalise(path)
        conn = self._connect()
        if conn is None:
            return []
        sql = f"SELECT {_COLUMNS} FROM entries"
        params: tuple = ()
        if path:
            sql += " WHERE path >= ? AND path < ?"
            params = (f"{path}/", f"{path}0")
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return [Entry.from_row(r) for r in rows]

    def child_count(self, path: str) -> tuple[int, int]:
        """`(rows, bytes)` directly under `path`, directories included: the filter
        counter's denominator (`26 → 9 matches`)."""
        conn = self._connect()
        if conn is None:
            return (0, 0)
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM entries "
                "WHERE parent IS ?",
                (path,),
            ).fetchone()
        except sqlite3.Error:
            return (0, 0)
        finally:
            conn.close()
        return (row[0], row[1])

    def max_file_size(self, paths: Sequence[str]) -> int:
        """Largest single *file* at or under any of `paths`, for the FAT32 4 GiB warning.
        Not `Entry.size`, which is a directory's recursive total: that warned about 68.7 GB
        in a library whose largest file is 786 MB. `is_dir = 0` is the point."""
        if not paths:
            return 0
        conn = self._connect()
        if conn is None:
            return 0
        clauses = []
        params: list[object] = []
        for raw in paths:
            path = normalise(raw)
            if not path:
                # The root: every file is under it.
                clauses = []
                params = []
                break
            # A half-open range, not LIKE: see `Manifests.presence` (6.72 -> 0.16 ms here).
            clauses.append("(path = ? OR (path >= ? AND path < ?))")
            params += [path, f"{path}/", f"{path}0"]

        sql = "SELECT COALESCE(MAX(size), 0) FROM entries WHERE is_dir = 0"
        if clauses:
            sql += " AND (" + " OR ".join(clauses) + ")"
        try:
            row = conn.execute(sql, params).fetchone()
        except sqlite3.Error:
            return 0
        finally:
            conn.close()
        return int(row[0] or 0)

    def ancestors(self, path: str) -> list[Entry]:
        """Root-first chain for the breadcrumb, excluding `path` itself."""
        out: list[Entry] = []
        parts = [p for p in path.split("/") if p]
        acc = ""
        for part in parts[:-1]:
            acc = f"{acc}/{part}" if acc else part
            found = self.entry(acc)
            if found:
                out.append(found)
        return out

    def vault_totals(self) -> tuple[int, int]:
        """``(files, bytes)`` of the vault, for a mirror's estimate, without walking it: the
        distinct hashes the index records are the vault's contents. DISTINCT, because two
        paths sharing a blob are one vault file. A floor: urantia-library/ is not indexed."""
        conn = self._connect()
        if conn is None:
            return (0, 0)
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM "
                "(SELECT DISTINCT blob, size FROM entries "
                " WHERE blob IS NOT NULL AND is_dir = 0)"
            ).fetchone()
        except sqlite3.Error:
            return (0, 0)
        finally:
            conn.close()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def all_file_paths(self) -> set[str]:
        """Every file path in the library, for set comparisons against a device."""
        conn = self._connect()
        if conn is None:
            return set()
        try:
            return {r[0] for r in conn.execute("SELECT path FROM entries WHERE is_dir = 0")}
        except sqlite3.Error:
            return set()
        finally:
            conn.close()

    # --- writing ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def reindex(self) -> IndexMeta:
        """Rebuild the index. Blocking — call this on a worker thread."""
        with self._lock:
            if self._running:
                return self.meta()
            self._running = True
        started = time.time()
        tmp = self.db_path.with_suffix(".db.tmp")
        try:
            tmp.unlink(missing_ok=True)
            tmp.parent.mkdir(parents=True, exist_ok=True)
            # uri=True so _enrich can ATTACH the catalog read-only by URI.
            conn = sqlite3.connect(tmp, uri=True)
            try:
                conn.executescript(SCHEMA)
                counters = _Counters()
                insert_sql = (
                    "INSERT OR REPLACE INTO entries "
                    f"({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                )
                with conn:
                    _walk(
                        self.root,
                        counters,
                        lambda rows: conn.executemany(insert_sql, rows),
                    )
                enriched = _enrich(conn, self.settings.catalog_db)
                duration = time.time() - started
                with conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)",
                        [
                            ("indexed_at", str(time.time())),
                            ("entry_count", str(counters.entries)),
                            ("file_count", str(counters.files)),
                            ("total_bytes", str(counters.total_bytes)),
                            ("duration", f"{duration:.2f}"),
                            ("errors", str(counters.errors)),
                            ("enriched", str(enriched)),
                            ("root", str(self.root)),
                        ],
                    )
                conn.execute("PRAGMA optimize")
            finally:
                conn.close()
            # Atomic publish. Readers holding the old inode finish undisturbed.
            os.replace(tmp, self.db_path)
            self._last_error = None
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI, never fatal
            if str(exc) != self._last_error:
                # Once per distinct fault, not every reindex_interval.
                log.warning("library reindex failed: %s", exc)
            self._last_error = str(exc)
            tmp.unlink(missing_ok=True)
        finally:
            self._running = False
        return self.meta()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class _Counters:
    def __init__(self) -> None:
        self.entries = 0
        self.files = 0
        self.total_bytes = 0
        self.errors = 0


def blob_from_link(target: str) -> str | None:
    """The vault hash a symlink target names, or None. Shared with `scan.parse_line`,
    which reads link targets out of a CAS node's listing."""
    base = os.path.basename(target)
    return base if _BLOB_RE.match(base) else None


def _blob_of(dir_entry: os.DirEntry) -> str | None:
    """The vault hash a library symlink points at, if it points at one."""
    try:
        if not dir_entry.is_symlink():
            return None
        target = os.readlink(dir_entry.path)
    except OSError:
        return None
    return blob_from_link(target)


def _walk(
    root: Path,
    counters: _Counters,
    flush: Callable[[list[tuple]], None],
    batch_size: int = 2000,
) -> None:
    """Walk the library, handing `flush` batches of index rows. Directory rows carry
    recursive `files`/`size` totals, so the depth-first recursion pushes rows through a
    callback rather than yielding them."""
    batch: list[tuple] = []

    def descend(abs_dir: Path, rel_dir: str, depth: int) -> tuple[int, int]:
        n_files = 0
        n_bytes = 0
        try:
            scanner = os.scandir(abs_dir)
        except OSError:
            counters.errors += 1
            return (0, 0)

        with scanner:
            for item in scanner:
                if depth == 0 and item.name in SKIP_TOPLEVEL:
                    continue
                rel = f"{rel_dir}/{item.name}" if rel_dir else item.name
                try:
                    is_dir = item.is_dir(follow_symlinks=False)
                except OSError:
                    counters.errors += 1
                    continue

                if is_dir:
                    sub_files, sub_bytes = descend(Path(item.path), rel, depth + 1)
                    try:
                        mtime = int(item.stat(follow_symlinks=False).st_mtime)
                    except OSError:
                        mtime = 0
                    batch.append(
                        (rel, rel_dir, item.name, 1, None, sub_bytes, mtime,
                         sub_files, None, None, None)
                    )
                    counters.entries += 1
                    n_files += sub_files
                    n_bytes += sub_bytes
                else:
                    try:
                        # follow_symlinks=True: size and mtime belong to the vault blob.
                        st = item.stat()
                    except OSError:
                        counters.errors += 1  # dangling link
                        continue
                    ext = os.path.splitext(item.name)[1].lower().lstrip(".") or None
                    batch.append(
                        (rel, rel_dir, item.name, 0, ext, st.st_size, int(st.st_mtime),
                         None, _blob_of(item), None, None)
                    )
                    counters.entries += 1
                    counters.files += 1
                    counters.total_bytes += st.st_size
                    n_files += 1
                    n_bytes += st.st_size

                if len(batch) >= batch_size:
                    flush(batch)
                    batch.clear()

        return (n_files, n_bytes)

    descend(root, "", 0)
    if batch:
        flush(batch)


def _enrich(conn: sqlite3.Connection, catalog_db: Path) -> int:
    """Fold title/author in from urantia-library's catalog, keyed on the blob hash.
    Optional: a missing or locked `lib.db` costs two columns. Never written."""
    if not Path(catalog_db).exists():
        return 0
    try:
        conn.execute(
            "ATTACH DATABASE ? AS cat", (f"file:{catalog_db}?mode=ro",)
        )
    except sqlite3.Error:
        return 0
    try:
        with conn:
            cur = conn.execute(
                "UPDATE entries SET (title, author) = "
                "  (SELECT b.title, b.author FROM cat.books b WHERE b.id = entries.blob) "
                "WHERE blob IS NOT NULL "
                "  AND EXISTS (SELECT 1 FROM cat.books b WHERE b.id = entries.blob)"
            )
            return cur.rowcount or 0
    except sqlite3.Error:
        return 0
    finally:
        try:
            conn.execute("DETACH DATABASE cat")
        except sqlite3.Error:
            pass


__all__ = [
    "Entry",
    "IndexMeta",
    "blob_from_link",
    "LibraryIndex",
    "PathError",
    "SORTS",
    "normalise",
]
