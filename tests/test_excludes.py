"""A device's `excludes`: never credited to it, and drawn on the map as held back.

Every Full Sync of note9 and s4a recorded Audio/, Video/ and Zhurnaly/ as shipped -- 1,952
files rsync had excluded -- and the coverage map drew both devices at 100%, solid green
under all three, and nothing but a scan would ever have taken that back. Once the
manifest was honest the same columns read "not there", which looks exactly like a
device that is merely behind: so what the excludes hold back is its own state, a hatch,
and a device is measured against the share its excludes let it take.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

from libnodes.manifests import CoverageCell, CoverageRow
from libnodes.models import ExcludeRules


@pytest.fixture
def devices_file(settings) -> Path:
    """Overrides the shared fixture: `kobo` excludes Physics/, `phone` nothing of its own."""
    path = settings.resolved_devices_file
    path.write_text(
        """
defaults:
  timeout: 20
  retries: 0
  excludes: ["*.sdr/"]

devices:
  - id: kobo
    name: Test Kobo
    type: kobo
    host: 127.0.0.1
    target: /mnt/onboard/Books
    full_sync: true
    excludes: ["Physics/"]

  - id: phone
    name: Test Phone
    type: termux
    host: 127.0.0.1
    target: /sdcard/Books
    full_sync: true
""".lstrip(),
        encoding="utf-8",
    )
    return path


# ------------------------------------------------------------------ the rules --


@pytest.mark.parametrize(
    ("pattern", "path", "is_dir", "hit"),
    [
        ("Audio/", "Audio", True, True),
        # Unanchored: any depth. note9's `Video/` holds back Science/Programming/Video too.
        ("Audio/", "Music/Audio", True, True),
        ("Audio/", "Audio", False, False),  # a trailing `/` is directories only
        ("/Audio/", "Music/Audio", True, False),  # a leading one anchors at the root
        ("/CLAUDE.md", "CLAUDE.md", False, True),
        ("*.sdr/", "Fiction/Joyce/Ulysses.sdr", True, True),
        ("*.sdr/", "Fiction/Joyce/Ulysses.sdr", False, False),
        # A pattern with an inner `/` matches a run of whole trailing components.
        ("Physics/Landau.pdf", "Science/Physics/Landau.pdf", False, True),
        ("ysics/Landau.pdf", "Science/Physics/Landau.pdf", False, False),
        # `*` stops at `/` (fnmatch's does not); `**` does not.
        ("Science/*/Landau.pdf", "Science/Physics/Landau.pdf", False, True),
        ("Science/*/Landau.pdf", "Science/A/Physics/Landau.pdf", False, False),
        ("Science/**/Landau.pdf", "Science/A/Physics/Landau.pdf", False, True),
        ("?al.pdf", "Science/Chess/Tal.pdf", False, True),
        ("[!S]*", "Science", True, False),
        ("[!S]*", "Fiction", True, True),
        ("Tal\\*.pdf", "Tal*.pdf", False, True),
        ("Tal\\*.pdf", "Tall.pdf", False, False),
    ],
)
def test_an_exclude_matches_as_rsync_matches_it(pattern, path, is_dir, hit):
    assert ExcludeRules([pattern]).matches(path, is_dir) is hit


def test_an_excluded_directory_holds_back_everything_below_it(index):
    """rsync never descends into one, so its contents are not roots of their own."""
    assert [r.path for r in index.excluded_roots(["Physics/"])] == ["Science/Physics"]
    roots = index.excluded_roots(["Science/", "*.pdf"])
    assert [r.path for r in roots] == ["Fiction/Joyce/Ulysses.pdf", "Science"]


def test_the_koreader_sidecars_hold_back_nothing_in_the_library(index):
    """The fleet default rides on every push; it must not shrink anyone's share."""
    assert index.excluded_roots(["*.sdr/"]) == []


def test_the_excluded_roots_follow_a_new_index(index, library):
    assert index.excluded_roots(["Audio/"]) == []
    blob = "cd" * 32
    (library / ".data" / blob).write_bytes(b"song")
    (library / "Audio").mkdir()
    os.symlink(f"../.data/{blob}", library / "Audio" / "Song.flac")
    time.sleep(0.01)  # indexed_at is the cache key: let the clock move
    index.reindex()
    assert [r.path for r in index.excluded_roots(["Audio/"])] == ["Audio"]


# -------------------------------------------------------------- the manifest --


def test_a_push_does_not_credit_what_its_excludes_held_back(app):
    from libnodes.jobs import Job, full_sync_sources

    lib = app.state.lib
    sources = full_sync_sources(lib.settings)
    for device in ("kobo", "phone"):
        lib.jobs._update_manifest(Job(id=1, device_id=device, sources=sources, label="x"))

    kobo = lib.manifests.paths_for("kobo")
    assert "Science/Chess/Tal.pdf" in kobo
    assert not [p for p in kobo if p.startswith("Science/Physics")]
    assert "Science/Physics/Landau.pdf" in lib.manifests.paths_for("phone")


async def test_the_estimate_leaves_out_what_the_excludes_hold_back(app):
    lib = app.state.lib
    tal = lib.index.entry("Science/Chess/Tal.pdf")
    assert lib.jobs._estimate(["Science"], excludes=["Physics/"]) == (1, tal.size)
    assert lib.jobs._estimate(["Science/Physics"], excludes=["Physics/"]) == (0, 0)

    job = lib.jobs.submit(lib.devices.config.by_id["kobo"], ["Science"])
    assert (job.files_total, job.bytes_total) == (1, tal.size)


# ------------------------------------------------------------------ the view --


def test_a_device_holding_its_whole_share_is_complete_minus_excludes():
    row = CoverageRow(held=18, total=20, stale=0, scanned=True, excluded=2)
    assert row.state == "share"
    assert row.fill_excluded == 10.0
    assert row.fill_ok + row.fill_excluded == 100.0, "green up to the hatch, no gap"


def test_a_leftover_does_not_stand_in_for_a_missing_book():
    """A copy inside an excluded tree is real, and counted as held, but it is not part of
    the share: 18 held with one of them a leftover is one book short."""
    row = CoverageRow(held=18, total=20, stale=0, scanned=True, excluded=2, held_out=1)
    assert row.state == "partial"
    assert row.fill_ok + row.fill_excluded < 100.0


def test_a_device_holding_even_what_it_excludes_is_complete():
    row = CoverageRow(held=20, total=20, stale=0, scanned=True, excluded=2, held_out=2)
    assert (row.state, row.fill_excluded, row.fill_ok) == ("complete", 0.0, 100.0)


def test_a_wholly_excluded_folder_is_excluded_not_unknown():
    assert CoverageCell(held=0, total=5, stale=0, scanned=False, excluded=5).state == (
        "excluded"
    )
    left = CoverageCell(held=1, total=5, stale=0, scanned=True, excluded=5, held_out=1)
    assert left.state == "excluded"
    assert left.fill_excluded == 100.0 and left.fill_ok > 0, "leftovers over the hatch"


def test_a_device_without_excludes_is_drawn_as_before():
    row = CoverageRow(held=18, total=20, stale=0, scanned=True)
    assert (row.state, row.fill_excluded, row.fill_ok) == ("partial", 0.0, 90.0)


def _rows(html: str) -> dict[str, str]:
    grid = html.split('class="cov-legend"')[0]
    parts = re.split(r'<div class="cov-r cov-name">', grid)[1:]
    return {re.search(r'cov-dev">([^<]*)<', p).group(1): p for p in parts}


def _hold_all_but_physics(lib) -> None:
    for device in ("kobo", "phone"):
        lib.manifests.record_entries(
            device,
            [
                lib.index.entry(p)
                for p in (
                    "Fiction/Aldiss/White-Mars.epub",
                    "Fiction/Joyce/Ulysses.pdf",
                    "Science/Chess/Tal.pdf",
                )
            ],
        )


async def test_the_map_draws_what_the_excludes_hold_back(client, app):
    lib = app.state.lib
    _hold_all_but_physics(lib)

    r = await client.get("/lib/presence", params={"p": ""})
    assert "1 complete minus excludes" in r.text and "1 partial" in r.text
    rows = _rows(r.text)
    kobo, phone = rows["Test Kobo"], rows["Test Phone"]
    assert "excludes Science/<wbr>Physics · " in kobo
    assert 'class="cov-bar-excl"' in kobo
    assert re.search(r'cov-pct">100%<', kobo)
    assert "cov-sq is-share" in kobo, "the Science column is all but its Physics"
    assert "excludes" not in phone and "cov-bar-excl" not in phone
    assert "excluded by its config</span>" in r.text, "the legend"

    r = await client.get("/lib/presence", params={"p": "Science"})
    kobo = _rows(r.text)["Test Kobo"]
    assert re.search(r'title="[^"]*· Physics ·[^"]*all excluded by its config"', kobo)
    assert "cov-sq is-excluded" in kobo


async def test_a_leftover_inside_an_excluded_folder_still_counts(client, app):
    """Written before the exclude, never pushed again and never pruned: on the device."""
    lib = app.state.lib
    _hold_all_but_physics(lib)
    landau = lib.index.entry("Science/Physics/Landau.pdf")
    lib.manifests.record_entries("kobo", [landau])

    rows = _rows((await client.get("/lib/presence", params={"p": ""})).text)
    assert re.search(r'cov-n">\s*<span>4</span>', rows["Test Kobo"])
    assert re.search(r'cov-pct">100%<', rows["Test Kobo"])

    r = await client.get("/lib/presence", params={"p": "Science/Physics"})
    kobo = _rows(r.text)["Test Kobo"]
    assert "1 excluded" in r.text
    assert re.search(r'cov-pct">50.0%<', kobo), "of the whole, when it is all excluded"


async def test_a_file_under_an_exclude_says_so(client, app):
    r = await client.get("/lib/presence", params={"p": "Science/Physics/Landau.pdf"})
    rows = _rows(r.text)
    assert "excluded by its config" in rows["Test Kobo"]
    assert "excluded" not in rows["Test Phone"]


def test_a_long_excluded_path_may_break_after_each_slash():
    """`Science/Programming/Video` unbroken widened the device column into the bar at
    tablet width (800px, Nexus 10)."""
    from libnodes.templating import names

    four = ["Audio", "Video", "Zhurnaly", "Science/Programming/Video"]
    assert str(names(four)) == "Audio, Video, Zhurnaly +1 more"
    assert str(names(["Science/Programming/Video"])) == "Science/<wbr>Programming/<wbr>Video"
    assert str(names(["<b>"])) == "&lt;b&gt;"


def test_the_hatch_is_ruled_for_the_bar_and_the_square():
    from tests.test_theme import _css, _rule

    css = re.sub(r"/\*.*?\*/", "", _css(), flags=re.S)
    bar = _rule(css, ".cov-bar > .cov-bar-excl")
    assert "repeating-linear-gradient" in bar and "margin-left: auto" in bar

    square = next(
        body
        for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", css)
        if ".cov-sq.is-excluded" in selectors
    )
    assert "repeating-linear-gradient" in square
    assert "calc(100% - var(--fx, 0%))" in square
