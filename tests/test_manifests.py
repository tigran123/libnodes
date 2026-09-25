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

    The DIR badge, the PRESENT ON fraction and the job estimate must all mean "files". rsync
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
    """A directory named `100%` must stay a literal, whatever the predicate is.

    This caught a missing ESCAPE back when the query was a LIKE. The range form that
    replaced it has no wildcards to escape at all, which is the cheaper way to be right,
    so this now stands as the guard that nobody reintroduces one.
    """
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


def _presence_vm_steps(manifests, entries, device_ids) -> int:
    """SQLite VM instructions executed by one real `presence()` call.

    `presence` opens its own connection, so the progress handler is installed by wrapping
    `_connect`. That is what makes this a measurement of the query the app actually runs,
    rather than of a re-typed copy that could drift away from it.
    """
    steps = [0]
    real = manifests._connect

    def bump() -> int:
        steps[0] += 1
        return 0  # anything non-zero would abort the query mid-flight

    def counted():
        conn = real()
        conn.set_progress_handler(bump, 1)
        return conn

    manifests._connect = counted
    try:
        manifests.presence(entries, device_ids)
    finally:
        del manifests._connect
    return steps[0]


def _dir_entry(path: str, files: int):
    from libnodes.library import Entry

    parent, _, name = path.rpartition("/")
    return Entry(
        path=path,
        parent=parent,
        name=name or path,
        is_dir=True,
        fmt=None,
        size=0,
        mtime=0,
        files=files,
        blob=None,
        title=None,
        author=None,
    )


def test_a_directory_count_costs_the_subtree_not_the_whole_device(settings):
    """`PRESENT ON` for a directory must be an index range, not a LIKE prefix.

    SQLite cannot serve `path LIKE 'dir/%'` from an index here -- the ESCAPE clause
    disables the LIKE optimisation, and so does case_sensitive_like=OFF against a BINARY
    column -- so each of these queries scanned that device's whole slice of the manifest.
    The cost was priced by what the device holds rather than by the subtree asked about.
    On the live database that was 304 directories x 15 devices = 4,560 queries over
    268,692 rows: 18.10 s, against 30 ms for `path >= ? AND path < ?` on the primary key.

    The assertion is the *shape* of the cost and not a step count, because the absolute
    numbers are facts about one SQLite build: counting three files must not get dearer
    because the device holds ten times as many files somewhere else. Under the LIKE form
    it did -- 3.1k VM steps became 30.1k, measured. Not an EXPLAIN QUERY PLAN string
    either: SQLite has reworded that output before (`SEARCH TABLE x USING...` became
    `SEARCH x USING...` in 3.36), which would fail while the code was right.
    """
    manifests = Manifests(settings.manifests_db)
    target = _dir_entry("Target", files=3)

    manifests.record(
        "kobo", [(f"Target/{n}.epub", None, 1, 0) for n in range(3)], source="push"
    )
    manifests.record(
        "kobo", [(f"Other/{n:05d}.epub", None, 1, 0) for n in range(500)], source="push"
    )
    small = _presence_vm_steps(manifests, [target], ["kobo"])

    manifests.record(
        "kobo",
        [(f"Other/{n:05d}.epub", None, 1, 0) for n in range(500, 5_000)],
        source="push",
    )
    large = _presence_vm_steps(manifests, [target], ["kobo"])

    assert manifests.presence([target], ["kobo"])["Target"][0].detail == "3/3"
    # The range plan is flat, so the honest delta is ~0; the slack is for the noise a
    # larger b-tree adds to the seek itself. A LIKE regression overshoots it ~100x.
    assert large <= small + 200


def test_a_directory_does_not_borrow_files_from_a_case_variant_sibling(settings):
    """LIKE was case-insensitive; the index range is exact, and that is the point.

    `Fiction/Abramov` used to count the files under `Fiction/abramov/` as its own, so a
    directory the device had never been sent could read as fully present. Two directories
    that differ only in case are two directories -- and this fleet's devices are vfat, so
    a scan really can bring a differently-cased path back.
    """
    manifests = Manifests(settings.manifests_db)
    manifests.record("kobo", [("Fiction/abramov/b.epub", None, 1, 0)], source="push")

    upper = _dir_entry("Fiction/Abramov", files=1)
    assert manifests.presence([upper], ["kobo"])["Fiction/Abramov"] == []

    lower = _dir_entry("Fiction/abramov", files=1)
    assert manifests.presence([lower], ["kobo"])["Fiction/abramov"][0].presence == "ok"


def test_the_unread_secondary_indexes_are_dropped_and_stay_dropped(settings):
    """Neither secondary index was ever read, and SCHEMA alone could not retire them.

    `PRIMARY KEY (device_id, path)` answers every `device_id = ?` lookup in the module,
    and better -- it carries `path`, so it needs no table lookup per row. It answers the
    one statement that does not lead with `device_id` as well: `presence`'s batched
    `path IN (...) AND device_id IN (...)` planned identically with and without
    ix_manifest_path on a copy of the live database, 3.65 ms against 3.46 ms. What they
    cost was writes -- 20,000 recorded scan rows, 66 ms against 47 ms -- and 39 MiB.

    Every statement in SCHEMA is IF NOT EXISTS, so it could only stop creating them on a
    fresh database and would have left the live 117 MiB one carrying both for ever. The
    DROPs in `_ensure` are what actually remove them, and SCHEMA must not put them back on
    the next open.
    """
    import sqlite3

    db = settings.manifests_db
    Manifests(db)  # create the schema

    def indexes() -> set[str]:
        conn = sqlite3.connect(db)
        try:
            return {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = 'manifest' AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            conn.close()

    legacy = {"ix_manifest_device": "device_id", "ix_manifest_path": "path"}
    conn = sqlite3.connect(db)
    try:
        for name, column in legacy.items():
            conn.execute(f"CREATE INDEX {name} ON manifest({column})")
        conn.commit()
    finally:
        conn.close()
    assert indexes() == set(legacy)

    Manifests(db)
    assert indexes() == set()
    Manifests(db)
    assert indexes() == set()


def test_the_presence_map_has_one_slot_per_device_in_fleet_order(settings, index):
    """The alignment the Library's presence map is entirely built on.

    `presence` appends a state only where there is evidence -- it never constructs
    `absent` -- so its list is dense and its length varies from row to row. That is right
    for a list of chips, each carrying its own name, and exactly wrong for a strip of
    anonymous slots whose only claim is that the third one is the same device on every
    row. Render the dense list and every slot after a device with nothing recorded shifts
    left, so the map says a book is on `phone` when it is on `kobo`.

    Nothing about that failure is visible: the page renders, the colours are plausible,
    and no request errors. This test is the only thing between it and the screen.
    """
    from libnodes.manifests import presence_slots

    manifests = Manifests(settings.manifests_db)
    entry = index.entry("Fiction/Joyce/Ulysses.pdf")
    manifests.record_entries("kobo", [entry])

    fleet = ["phone", "kobo", "tablet"]
    slots = presence_slots(manifests.presence([entry], fleet), fleet)[entry.path]

    assert len(slots) == len(fleet)
    assert slots[0] is None
    assert slots[1] is not None and slots[1].device_id == "kobo"
    assert slots[2] is None

    # And the order is the caller's, not the database's.
    reversed_fleet = list(reversed(fleet))
    other = presence_slots(manifests.presence([entry], reversed_fleet), reversed_fleet)
    assert [s.device_id if s else None for s in other[entry.path]] == [
        None,
        "kobo",
        None,
    ]


def test_a_partial_directory_is_not_drawn_like_a_full_one(settings, index):
    """`partial` and `absent` shared the plain `.badge` class, so "2 of 900 files" and
    "all 900" were the same picture and the difference lived in a tooltip. A slot 4px
    wide has no tooltip to fall back on."""
    manifests = Manifests(settings.manifests_db)
    directory = index.entry("Science/Physics")
    one = index.entry("Science/Physics/Landau.pdf")
    manifests.record_entries("kobo", [one])

    state = manifests.presence([directory], ["kobo"])[directory.path][0]
    assert state.presence == "partial"
    assert len({state.map_class, "p-ok", "p-none", "p-stale"}) == 4
