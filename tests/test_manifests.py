"""PRESENT ON: what each device holds, and whether its copy is still current."""

from __future__ import annotations

from libnodes.manifests import Manifests


def test_push_then_present(settings, index):
    manifests = Manifests(settings.manifests_db)
    entry = index.entry("Fiction/Joyce/Ulysses.pdf")
    manifests.record_entries("kobo", [entry])

    states = manifests.presence([entry], ["kobo", "phone"])
    assert [s.device_id for s in states[entry.path]] == ["kobo"]
    assert states[entry.path][0].presence == "ok"


def test_absent_on_other_devices(settings, index):
    manifests = Manifests(settings.manifests_db)
    entry = index.entry("Fiction/Joyce/Ulysses.pdf")
    manifests.record_entries("kobo", [entry])
    states = manifests.presence([entry], ["phone"])
    assert states[entry.path] == []


def test_stale_when_content_hash_changes(settings, index):
    """Content addressing makes staleness exact — no size/mtime guessing."""
    manifests = Manifests(settings.manifests_db)
    entry = index.entry("Fiction/Joyce/Ulysses.pdf")
    manifests.record(
        "kobo", [(entry.path, "0" * 128, entry.size, entry.mtime)], source="push"
    )
    states = manifests.presence([entry], ["kobo"])
    assert states[entry.path][0].presence == "stale"


def test_same_size_and_mtime_but_different_content_is_still_stale(settings, index):
    """The case a size+mtime comparison would miss entirely."""
    manifests = Manifests(settings.manifests_db)
    entry = index.entry("Fiction/Joyce/Ulysses.pdf")
    manifests.record(
        "kobo",
        [(entry.path, "f" * 128, entry.size, entry.mtime)],
        source="push",
    )
    assert manifests.presence([entry], ["kobo"])[entry.path][0].presence == "stale"


def test_directory_shows_partial_until_complete(settings, index):
    manifests = Manifests(settings.manifests_db)
    physics = index.entry("Science/Physics")
    one = index.entry("Science/Physics/Landau.pdf")

    manifests.record_entries("kobo", [one])
    states = manifests.presence([physics], ["kobo"])
    assert states[physics.path][0].presence == "partial"
    assert states[physics.path][0].detail == "1/2"

    manifests.record_entries("kobo", [index.entry("Science/Physics/Feynman.djvu")])
    states = manifests.presence([physics], ["kobo"])
    assert states[physics.path][0].presence == "ok"


def test_every_view_counts_files_the_same_way(settings, index, app):
    """One directory, one file count, wherever it is displayed.

    The tree, the PRESENT ON fraction and the job estimate must all mean "files". rsync
    disagrees on purpose — its file list counts directories, so a directory of 234 files
    with 9 subdirectories is 244 entries to it — and letting that number leak into a
    view that says "files" is how the same directory came to read 234 in one place and
    244 in another.
    """
    manifests = Manifests(settings.manifests_db)
    science = index.entry("Science")
    files = [e for e in _descend(index, "Science") if not e.is_dir]

    manifests.record_entries("kobo", files)
    detail = manifests.presence([science], ["kobo"])[science.path][0].detail
    estimated_files, _ = app.state.lib.jobs._estimate(["Science"])

    assert science.files == len(files)
    assert detail == f"{len(files)}/{science.files}"
    assert estimated_files == science.files


def _descend(index, path):
    for child in index.children(path, limit=1000):
        yield child
        if child.is_dir:
            yield from _descend(index, child.path)


def test_scan_replaces_previous_scan_rows(settings, index):
    manifests = Manifests(settings.manifests_db)
    manifests.replace_scan("kobo", [("Fiction/Old.pdf", None, 10, 0)])
    assert manifests.summary("kobo")[0] == 1
    manifests.replace_scan("kobo", [("Fiction/New.pdf", None, 20, 0)])
    rows = manifests.rows_for("kobo")
    assert [r.path for r in rows] == ["Fiction/New.pdf"]
    assert rows[0].source == "scan"


def test_forget_clears_a_device(settings, index):
    manifests = Manifests(settings.manifests_db)
    manifests.record_entries("kobo", [index.entry("Fiction/Joyce/Ulysses.pdf")])
    manifests.forget("kobo")
    assert manifests.summary("kobo")[0] == 0


def test_paths_with_sql_wildcards_do_not_leak(settings, index, tmp_path):
    """A directory named `100%` must not match every sibling via LIKE."""
    manifests = Manifests(settings.manifests_db)
    manifests.record("kobo", [("Other/file.pdf", None, 1, 0)], source="push")

    from libnodes.library import Entry

    tricky = Entry(
        path="100%",
        parent="",
        name="100%",
        is_dir=True,
        fmt=None,
        size=0,
        mtime=0,
        files=1,
        blob=None,
        title=None,
        author=None,
    )
    assert manifests.presence([tricky], ["kobo"])["100%"] == []
