"""Fixtures: a miniature content-addressed library, and an app pointed at it.

The fixture tree mirrors the real `/Books` layout exactly — symlinks into a `.data/`
vault of blake2b-named blobs — because that shape is what most of the interesting
behaviour depends on.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest


def _blob_name(payload: bytes) -> str:
    return hashlib.blake2b(payload).hexdigest()


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """A small /Books lookalike: symlinks pointing into a CAS vault."""
    root = tmp_path / "books"
    data = root / ".data"
    data.mkdir(parents=True)

    def add(rel: str, payload: bytes) -> str:
        blob = _blob_name(payload)
        (data / blob).write_bytes(payload)
        link = root / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        depth = len(Path(rel).parts) - 1
        link.symlink_to(Path(*[".."] * depth) / ".data" / blob)
        return blob

    add("Fiction/Aldiss/White-Mars.epub", b"epub payload" * 40)
    add("Fiction/Joyce/Ulysses.pdf", b"pdf payload" * 100)
    add("Science/Physics/Feynman.djvu", b"djvu payload" * 60)
    add("Science/Physics/Landau.pdf", b"landau" * 200)
    add("Science/Chess/Tal.pdf", b"tal" * 33)

    # Infrastructure that must never appear in the browsable tree.
    (root / "urantia-library").mkdir()
    (root / "urantia-library" / "secrets.env").write_text("TOKEN=nope")
    (root / "CLAUDE.md").write_text("# notes")
    (data / "covers").mkdir()
    (data / "covers" / "x.jpg").write_bytes(b"jpg")

    # A dangling link: the walk must survive it rather than abort.
    (root / "Science" / "missing.pdf").symlink_to("../.data/deadbeef")

    return root


@pytest.fixture
def settings(library: Path, tmp_path: Path, monkeypatch):
    from libnodes.config import get_settings, reset_caches

    monkeypatch.setenv("LIBNODES_LIBRARY_ROOT", str(library))
    monkeypatch.setenv("LIBNODES_STATE_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("LIBNODES_CATALOG_DB", str(tmp_path / "no-such-catalog.db"))
    monkeypatch.setenv("LIBNODES_REINDEX_ON_START", "false")
    monkeypatch.setenv("LIBNODES_REINDEX_INTERVAL", "0")
    monkeypatch.setenv("LIBNODES_PROBE_INTERVAL", "3600")
    reset_caches()
    settings = get_settings()
    settings.ensure_dirs()
    yield settings
    reset_caches()


@pytest.fixture
def index(settings):
    from libnodes.library import LibraryIndex

    ix = LibraryIndex(settings)
    ix.reindex()
    return ix


@pytest.fixture
def devices_file(settings) -> Path:
    path = settings.resolved_devices_file
    path.write_text(
        """
defaults:
  rsync_flags: ["-avhP", "--partial", "--info=progress2"]
  timeout: 20
  retries: 0

devices:
  - id: kobo
    name: Test Kobo
    abbr: TK
    type: kobo
    host: 127.0.0.1
    port: 2222
    user: root
    target: /mnt/onboard/Books
    full_sync: true
    capacity: 29G
    formats: [epub, pdf]

  - id: phone
    name: Test Phone
    abbr: PH
    type: termux
    host: 127.0.0.1
    port: 8022
    user: u0_a1
    target: /sdcard/Books
""".lstrip(),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def app(settings, devices_file, index):
    from libnodes.config import get_devices
    from libnodes.main import create_app

    get_devices.cache_clear()
    application = create_app()
    application.state.lib.index.reindex()
    return application


@pytest.fixture
async def client(app):
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            yield c


@pytest.fixture
def fake_rsync(tmp_path: Path) -> Path:
    """A stand-in rsync that emits real --info=progress2 output, then exits 0.

    Lets the runner, the parser and the SSE fan-out be tested end to end without a
    network, a device, or a 250 GB library.
    """
    script = tmp_path / "fake-rsync"
    script.write_text(
        "#!/bin/sh\n"
        "echo 'sending incremental file list'\n"
        "echo 'Fiction/Aldiss/White-Mars.epub'\n"
        "printf '        480 100%%    0.47MB/s    0:00:00 (xfr#1, to-chk=1/2)\\r'\n"
        "echo 'Fiction/Joyce/Ulysses.pdf'\n"
        "printf '       1,580 100%%    1.51MB/s    0:00:01 (xfr#2, to-chk=0/2)\\r'\n"
        "echo ''\n"
        "echo 'sent 2,060 bytes  received 57 bytes  4,234.00 bytes/sec'\n"
        "exit 0\n"
    )
    script.chmod(0o755)
    return script
