"""The BATTERY column: read from a per-device file, drawn like STORAGE.

There is no portable way to ask a device for its charge, so `battery:` in devices.yaml
names the file to read. These tests pin the two things that would otherwise break
quietly: what counts as a reading, and that the column the CSS declares is the column the
templates fill.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from libnodes.probe import Battery, _parse_battery, _readings_script, _section

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------- reading it --


@pytest.mark.parametrize(
    "text,expected",
    [
        ("100\n", 100),
        ("87", 87),
        ("0\n", 0),
        ("  42  \n", 42),
        # A sysfs node that is not there prints an error. Pattern-matching a number out
        # of one would read "2" from "No such file or directory (2)" and put a confident
        # wrong figure on the row.
        ("cat: /sys/.../capacity: No such file or directory\n", None),
        ("", None),
        ("\n\n", None),
        # Out of range is not a percentage, whatever the file meant by it.
        ("101\n", None),
        ("4000000\n", None),
        # Some nodes report microvolts or a status word; neither is a percentage.
        ("Charging\n", None),
        ("3.7V\n", None),
    ],
)
def test_parse_battery(text, expected):
    assert _parse_battery(text) == expected


def test_the_battery_rides_along_with_df():
    """One ssh, not two. On a sleeping Termux node the connection is the cost, and two
    probes on separate schedules would also drift apart in a row that shows both."""
    script = _readings_script("df -Pk /sdcard/Books", "/sys/class/power_supply/battery/capacity")
    assert script.count("df -Pk") == 1
    assert "# df" in script and "# battery" in script
    assert "/sys/class/power_supply/battery/capacity" in script

    # A device with no battery declared gets the bare df it always got — no marker
    # scaffolding, nothing extra to parse.
    assert _readings_script("df -Pk /sdcard/Books", None) == "df -Pk /sdcard/Books"


def test_the_sections_do_not_bleed_into_each_other():
    """`_parse_df` scans backwards for the last line that parses. A bare `100` on its own
    line cannot be a df record, but the marker keeps the question from arising."""
    out = (
        "# df\n"
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/block/vold 30408704 7969472 22439232 27% /storage/sdcard1\n"
        "# battery\n100\n"
    )
    assert _section(out, "battery").strip() == "100"
    assert "100\n" not in _section(out, "df")
    assert "30408704" in _section(out, "df")


# --------------------------------------------------------------- the slot --


async def test_a_bad_read_keeps_the_last_known_figure(app):
    """A device that answers `df` but whose battery node has gone away should not blank
    a bar that was right a minute ago — the error is what says it has stopped updating."""
    probe = app.state.lib.probe
    probe._record_battery("kobo", "64\n")
    assert probe.battery("kobo").percent == 64
    assert probe.battery("kobo").error is None

    probe._record_battery("kobo", "cat: /sys/nope: No such file or directory\n")
    later = probe.battery("kobo")
    assert later.percent == 64, "one bad read threw away a good figure"
    assert "No such file" in later.error
    assert later.checked_at is not None


async def test_a_device_without_a_battery_is_never_asked(app, monkeypatch):
    """`battery:` unset means the fleet has nodes with no battery worth reporting, not
    that we should go looking. The command must be the plain df it always was."""
    commands = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return (b"", b"")

    async def spy(*argv, **k):
        commands.append(argv[-1])
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", spy)
    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"]
    assert device.battery is None

    from libnodes.probe import Reachability

    now = time.time()
    lib.probe._slot("kobo").reach = Reachability(state="online", last_ok=now,
                                                 checked_at=now)
    await lib.probe.probe_space(device, force=True)

    assert commands, "no command ran at all"
    assert all("# battery" not in c for c in commands)
    assert all("power_supply" not in c for c in commands)


# ---------------------------------------------------------------- the row --


async def test_the_row_draws_the_charge_it_read(client, app):
    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"]
    object.__setattr__(device, "battery", "/sys/class/power_supply/battery/capacity")
    lib.probe._slot("kobo").battery = Battery(percent=64, checked_at=time.time())

    row = (await client.get("/devices/rows")).text
    assert "64%" in row
    assert 'style="width:64.0%"' in row


async def test_a_low_battery_is_tinted_and_a_healthy_one_is_not(client, app):
    """Storage fills up as it gets worse and a battery empties, so they cannot share a
    threshold. `track-warn`/`track-err` are colour modifiers composed onto `track-2`,
    which is a height — asserting the pair keeps them from being conflated again."""
    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"]
    object.__setattr__(device, "battery", "/sys/class/power_supply/battery/capacity")

    lib.probe._slot("kobo").battery = Battery(percent=9, checked_at=time.time())
    assert "track track-2 track-err" in (await client.get("/devices/rows")).text

    lib.probe._slot("kobo").battery = Battery(percent=22, checked_at=time.time())
    assert "track track-2 track-warn" in (await client.get("/devices/rows")).text

    lib.probe._slot("kobo").battery = Battery(percent=80, checked_at=time.time())
    row = (await client.get("/devices/rows")).text
    assert "track-err" not in row
    assert "track-warn" not in row

    css = (ROOT / "libnodes" / "static" / "app.css").read_text()
    assert ".track-warn > i" in css, "the tint the row asks for is not defined"


async def test_a_device_with_no_battery_declared_shows_nothing(client, app):
    """Not a zero and not a bar — the column is empty for a node nobody told us how to
    ask, because a missing reading and a flat battery are different facts."""
    lib = app.state.lib
    assert lib.devices.config.by_id["kobo"].battery is None
    row = (await client.get("/devices/rows")).text
    assert "%</span>" not in row.split('data-label="Battery"')[1].split("</div>")[0]


# ------------------------------------------------------------- the layout --


def test_the_grid_declares_a_track_for_every_cell():
    """A CSS grid silently drops a cell onto a second row when the template grows a
    column the stylesheet does not know about — the row still renders, just wrong."""
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()
    block = css.split(".device-grid {")[1].split("}")[0]
    tracks = len(re.findall(r"minmax\(", block))

    head = (ROOT / "libnodes" / "templates" / "devices.html").read_text()
    thead = head.split('class="thead device-grid"')[1].split("</div>\n      </div>")[0]
    headers = len(re.findall(r"<div[^>]*>[A-Z]", thead))

    # Every grid cell carries data-label -- the stacked layout under 1200px prints it as
    # the row's heading. Matched anywhere in the tag, because `cell-actions` puts its
    # class first.
    row = (ROOT / "libnodes" / "templates" / "device_row.html").read_text()
    cells = len(re.findall(r"^  <div [^>]*data-label=", row, re.MULTILINE))

    assert tracks == headers == cells, (
        f"{tracks} CSS tracks, {headers} headers, {cells} cells — these must agree"
    )

    # The one top-level div that is not a column has to say so, or it takes a track and
    # pushes Actions onto a second row.
    css_subrow = css.split(".subrow {")[1].split("}")[0]
    assert "grid-column: 1 / -1" in css_subrow
