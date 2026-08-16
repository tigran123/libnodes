"""The cached library index.

`/Books` is content-addressed: every book is a symlink into `/Books/.data/<blake2b>`,
so the browsable tree is 20.8k symlinks and the bytes live once in the vault. Two
consequences run through this module:

* `stat()` must follow the link (sizes come from the vault blob, not the 100-byte
  symlink), which `os.DirEntry.stat()` does by default.
* the link target's basename is the blob hash, which is a free exact content identity —
  we store it, and both the manifest staleness check and the optional catalog join key
  off it.

Nothing here is ever called from a request handler while it walks. A full walk of the
real library measures ~29s on the Pi, so reindexing runs on a single background thread
and publishes by atomic rename; readers open short-lived read-only connections and
never see a half-built index.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from .config import SKIP_TOPLEVEL, Settings

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
CREATE INDEX ix_entries_fmt    ON entries(fmt);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
"""

SORTS = {
    "name": "is_dir DESC, name COLLATE NOCASE ASC",
    "name_desc": "is_dir DESC, name COLLATE NOCASE DESC",
    "size": "is_dir DESC, size DESC",
    "size_asc": "is_dir DESC, size ASC",
    "modified": "is_dir DESC, mtime DESC",
    "modified_asc": "is_dir DESC, mtime ASC",
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
    def hidden(self) -> bool:
        """Dot-entries render in `faint` with no affordances, per the design."""
        return self.name.startswith(".")

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
            return IndexMeta(None, 0, 0, 0, None, 0, self._running)
        try:
            rows = dict(conn.execute("SELECT k, v FROM meta").fetchall())
        except sqlite3.Error:
            return IndexMeta(None, 0, 0, 0, None, 0, self._running)
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

    def abs_path(self, path: str) -> Path:
        return self.root / path if path else self.root

    def children(
        self,
        path: str,
        *,
        q: str | None = None,
        fmts: Sequence[str] | None = None,
        sort: str = "name",
        limit: int = 2000,
        dirs_only: bool = False,
    ) -> list[Entry]:
        """Rows for the file table.

        With no query this is the directory listing. With a query it becomes a
        recursive search of the subtree, which at the root means the whole library —
        that is what makes the filter box useful on 20.8k entries.
        """
        conn = self._connect()
        if conn is None:
            return []
        where = []
        params: list[object] = []

        if q:
            if path:
                where.append("(path = ? OR path LIKE ?)")
                params += [path, f"{path}/%"]
            where.append("name LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(q)}%")
            where.append("is_dir = 0")
        else:
            where.append("parent IS ?")
            params.append(path)

        if dirs_only:
            where.append("is_dir = 1")
        if fmts:
            where.append("fmt IN (%s)" % ",".join("?" * len(fmts)))
            params += [f.lower() for f in fmts]

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

    def child_count(self, path: str) -> tuple[int, int]:
        """`(rows, bytes)` directly under `path`.

        Counts directories as well as files, because this is the denominator the filter
        counter reports (`26 → 9 matches`) and a directory of directories would
        otherwise claim to hold nothing.
        """
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
        """Largest single *file* at or under any of `paths`.

        For the FAT32 warning, which is about one file exceeding 4 GiB. `Entry.size`
        cannot answer it: on a directory that column holds the recursive total, so a
        selection of the 17 top-level directories reported its largest file as 68.7 GB —
        the size of `Science` entire — against a library whose biggest actual file is
        786 MB. Every directory push therefore carried a red FAT32 warning that was
        arithmetic on the wrong number.

        `is_dir = 0` is the whole point of the query; do not drop it.
        """
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
                # The library root is selected: every file is under it, so the prefix
                # clauses would only narrow what is already the whole table.
                clauses = []
                params = []
                break
            clauses.append("(path = ? OR path LIKE ? ESCAPE '\\')")
            params += [path, f"{_escape_like(path)}/%"]

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

    def expanded_tree(self, selected: str) -> list[tuple[Entry, int, bool]]:
        """The tree pane: root's children plus every level along `selected`.

        Returns `(entry, depth, expanded)`. Only the open path is materialised, so the
        pane costs one small query per open level rather than a walk.
        """
        open_paths = {""}
        acc = ""
        for part in [p for p in selected.split("/") if p]:
            acc = f"{acc}/{part}" if acc else part
            open_paths.add(acc)

        out: list[tuple[Entry, int, bool]] = []

        def emit(parent: str, depth: int) -> None:
            for child in self.children(parent, dirs_only=True, limit=500):
                expanded = child.path in open_paths
                out.append((child, depth, expanded))
                if expanded:
                    emit(child.path, depth + 1)

        emit("", 0)
        return out

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

    def blobs_present(self, blobs: Iterable[str]) -> set[str]:
        conn = self._connect()
        if conn is None:
            return set()
        wanted = list(blobs)
        found: set[str] = set()
        try:
            for chunk in _chunks(wanted, 400):
                sql = "SELECT blob FROM entries WHERE blob IN (%s)" % ",".join(
                    "?" * len(chunk)
                )
                found.update(r[0] for r in conn.execute(sql, chunk))
        except sqlite3.Error:
            return set()
        finally:
            conn.close()
        return found

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
            self._last_error = str(exc)
            tmp.unlink(missing_ok=True)
        finally:
            self._running = False
        return self.meta()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _chunks(seq: Sequence, size: int) -> Iterator[Sequence]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


class _Counters:
    def __init__(self) -> None:
        self.entries = 0
        self.files = 0
        self.total_bytes = 0
        self.errors = 0


def _blob_of(dir_entry: os.DirEntry) -> str | None:
    """The vault hash a library symlink points at, if it points at one."""
    try:
        if not dir_entry.is_symlink():
            return None
        base = os.path.basename(os.readlink(dir_entry.path))
    except OSError:
        return None
    return base if _BLOB_RE.match(base) else None


def _walk(
    root: Path,
    counters: _Counters,
    flush: "Callable[[list[tuple]], None]",
    batch_size: int = 2000,
) -> None:
    """Walk the library, handing `flush` batches of index rows.

    Directory rows carry recursive `files`/`size` aggregates so the tree pane can print
    `4,812 · 61G` without a query per node. The recursion has to be depth-first to
    compute those aggregates, so rows are pushed through a callback rather than yielded
    — a generator would have to flatten every level before emitting anything.
    """
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

    Entirely optional: a missing, locked or restructured `lib.db` costs us the two
    metadata columns and nothing else. We never write to it.
    """
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
    "LibraryIndex",
    "PathError",
    "SORTS",
    "normalise",
]
