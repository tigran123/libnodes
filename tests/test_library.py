"""The index has to understand that the library is symlinks into a CAS vault."""

from __future__ import annotations

import pytest

from libnodes.library import PathError, normalise


def test_walk_follows_symlinks_into_the_vault(index):
    """Sizes come from the blob, not from the 100-byte symlink."""
    entry = index.entry("Fiction/Joyce/Ulysses.pdf")
    assert entry is not None
    assert entry.size == len(b"pdf payload" * 100)
    assert entry.fmt == "pdf"
    assert not entry.is_dir


def test_blob_hash_is_recorded(index):
    entry = index.entry("Fiction/Aldiss/White-Mars.epub")
    assert entry.blob is not None
    # blake2b-512 hex
    assert len(entry.blob) == 128
    assert all(c in "0123456789abcdef" for c in entry.blob)


def test_directory_aggregates_are_recursive(index):
    physics = index.entry("Science/Physics")
    assert physics.is_dir
    assert physics.files == 2
    assert physics.size == (
        index.entry("Science/Physics/Feynman.djvu").size
        + index.entry("Science/Physics/Landau.pdf").size
    )

    science = index.entry("Science")
    # Physics(2) + Chess(1); the dangling link is skipped, not counted.
    assert science.files == 3


def test_infrastructure_is_not_browsable(index):
    for hidden in ("urantia-library", "CLAUDE.md", ".data"):
        assert index.entry(hidden) is None
    assert not any(
        e.name in ("urantia-library", "CLAUDE.md", ".data") for e in index.children("")
    )


def test_production_secrets_are_unreachable(index):
    """`urantia-library` is a security boundary, not housekeeping.

    On the Pi that path is not a source checkout — it is a copy of the production box
    behind sigmaai.au/library, holding a real webapp/secrets.env. It must be invisible
    to browsing, to search, and to the push path.
    """
    secret = "urantia-library/webapp/secrets.env"

    assert index.entry(secret) is None
    with pytest.raises(PathError):
        index.require(secret)

    # Not reachable by searching for it either, from any starting point.
    assert index.children("", q="secrets") == []
    assert index.children("", q="urantia") == []

    # And the directory itself is not a push source.
    with pytest.raises(PathError):
        index.require("urantia-library")


def test_full_sync_never_includes_infrastructure(settings, index):
    """Full Sync enumerates top-level dirs; the skiplist has to hold there too."""
    from libnodes.jobs import full_sync_sources

    sources = full_sync_sources(settings)
    assert sources  # the fixture library does have real categories
    for forbidden in ("urantia-library", ".data", "CLAUDE.md", "GEMINI.md"):
        assert forbidden not in sources


def test_dangling_symlink_is_skipped_not_fatal(index):
    assert index.entry("Science/missing.pdf") is None
    assert index.meta().errors >= 1
    assert index.meta().entry_count > 0


def test_filter_searches_the_subtree(index):
    assert [e.name for e in index.children("", q="Feynman")] == ["Feynman.djvu"]
    # Scoped: the same query under an unrelated subtree finds nothing.
    assert index.children("Fiction", q="Feynman") == []


def test_format_filter(index):
    pdfs = index.children("", q="a", fmts=["pdf"])
    assert pdfs
    assert {e.fmt for e in pdfs} == {"pdf"}


def test_sorting(index):
    by_size = index.children("Science/Physics", sort="size")
    assert by_size[0].size >= by_size[-1].size


@pytest.mark.parametrize(
    "bad",
    ["../etc/passwd", "/etc/passwd", "Science/../../etc", ".."],
)
def test_path_guard_rejects_traversal(index, bad):
    with pytest.raises(PathError):
        index.require(bad)


@pytest.mark.parametrize("root_form", ["", "/", ".", None])
def test_root_is_addressable(index, root_form):
    """`/` and `.` mean the library root — legitimate, not traversal."""
    assert index.require(root_form).path == ""


def test_path_guard_rejects_unindexed_paths(index):
    """The index is the whitelist — a real file that we chose not to index is still out.

    `.data` exists on disk and resolves inside the library root, so a naive
    `is_relative_to` check would wave it through.
    """
    with pytest.raises(PathError):
        index.require(".data")
    with pytest.raises(PathError):
        index.require("urantia-library/secrets.env")


def test_normalise():
    assert normalise(None) == ""
    assert normalise("") == ""
    assert normalise("/Science/") == "Science"
    assert normalise("Science//Physics") == "Science/Physics"


def test_expanded_tree_opens_only_the_selected_path(index):
    tree = index.expanded_tree("Science/Physics")
    opened = {e.path for e, _, expanded in tree if expanded}
    assert opened == {"Science", "Science/Physics"}
    names = [e.path for e, _, _ in tree]
    assert "Fiction" in names
    # Fiction is closed, so its children are not materialised.
    assert "Fiction/Joyce" not in names


def test_reindex_is_atomic(index, settings):
    """A rebuild republishes by rename; the old file is never truncated in place."""
    before = settings.index_db.stat().st_ino
    index.reindex()
    after = settings.index_db.stat().st_ino
    assert before != after
    assert not settings.index_db.with_suffix(".db.tmp").exists()


def test_recommended_is_a_category_not_a_place(settings, library):
    """`Recommended/` holds companion symlinks to books already in the tree.

    urantia-library manages it as a pseudo-directory. Because LibNodes transfers with
    -L, including it would ship a second full copy of every recommended book.
    """
    import os

    from libnodes.jobs import full_sync_sources
    from libnodes.library import LibraryIndex, PathError

    rec = library / "Recommended"
    rec.mkdir()
    # A companion symlink to a book that already exists under Fiction/.
    target = library / "Fiction" / "Joyce" / "Ulysses.pdf"
    os.symlink(os.path.relpath(target, rec), rec / "A recommended book.pdf")

    ix = LibraryIndex(settings)
    ix.reindex()

    assert ix.entry("Recommended") is None
    assert not any(e.name == "Recommended" for e in ix.children(""))
    with pytest.raises(PathError):
        ix.require("Recommended/A recommended book.pdf")
    assert "Recommended" not in full_sync_sources(settings)
