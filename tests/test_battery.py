"""The BATTERY column: read from a per-device file, drawn like STORAGE.

There is no portable way to ask a device for its charge, so `battery:` in devices.yaml
names the file to read. These tests pin the two things that would otherwise break
quietly: what counts as a reading, and that the column the CSS declares is the column the
templates fill.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import pytest

from libnodes.probe import Battery, _parse_battery, _readings_script, _section

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------- reading it --


#: What `termux-api BatteryStatus` actually printed on note10, verbatim.
TERMUX_JSON = """\
{
  "health": "GOOD",
  "percentage": 46,
  "plugged": "UNPLUGGED",
  "status": "DISCHARGING",
  "temperature": 31.700000762939453,
  "current": -596
}
"""


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
        # --- JSON, which is what Android 12 forces: /sys is unreadable from Termux ---
        (TERMUX_JSON, 46),
        ('{"capacity": 88}', 88),
        ('{"level": 72}', 72),
        ('{"battery_level": 5}', 5),
        ('{"Percentage": 33}', 33),          # key match is case-insensitive
        ('{"percentage": 46.4}', 46),        # a float source rounds
        # Nothing that means charge -- reported as an error, never guessed at. The
        # temperature and current in the real payload are exactly the numbers a looser
        # parser would have grabbed.
        ('{"temperature": 31.7, "current": -596}', None),
        ('{"volts": 3.7}', None),
        # `percentage_design` must not answer for `percentage`; keys match exactly.
        ('{"percentage_design": 80}', None),
        # true is an int in Python, and "charging" is not 1%.
        ('{"level": true}', None),
        ('{"percentage": 146}', None),
        ('{"percentage": "46"}', None),      # a string is not a number
        ("[46]", None),                      # JSON, but not an object
        ("{ not json at all", None),
    ],
)
def test_parse_battery(text, expected):
    assert _parse_battery(text) == expected


def _dev(**kw):
    from libnodes.models import Device

    base = dict(id="d", name="D", type="termux", host="h", target="/sdcard/Books")
    return Device(**{**base, **kw})


def test_the_battery_rides_along_with_df():
    """One ssh, not two. On a sleeping Termux node the connection is the cost, and two
    probes on separate schedules would also drift apart in a row that shows both."""
    device = _dev(battery="/sys/class/power_supply/battery/capacity")
    script = _readings_script(device, "df -Pk /sdcard/Books")
    assert script.count("df -Pk") == 1
    assert "# df" in script and "# battery" in script
    assert "/sys/class/power_supply/battery/capacity" in script

    # A device with no battery declared gets the bare df it always got — no marker
    # scaffolding, nothing extra to parse.
    assert _readings_script(_dev(), "df -Pk /sdcard/Books") == "df -Pk /sdcard/Books"


def test_a_command_is_run_not_quoted_as_a_filename():
    """battery_cmd is a command line and may be a pipeline. Quoting it the way the file
    path is quoted would run the whole string as the name of one program."""
    cmd = "/data/data/com.termux/files/usr/libexec/termux-api BatteryStatus"
    script = _readings_script(_dev(battery_cmd=cmd), "df /sdcard/Books")
    assert cmd in script                      # verbatim, argument and all
    assert f"'{cmd}'" not in script
    assert "cat " not in script
    assert "# battery" in script

    # The file form still is quoted — a path with a space must not become two words.
    spaced = _readings_script(_dev(battery="/sys/odd name/capacity"), "df /x")
    assert "'/sys/odd name/capacity'" in spaced


def test_a_device_cannot_declare_both_battery_sources():
    """A half-finished edit — someone moving a node from sysfs to termux-api and leaving
    the old line — must fail loudly. Any precedence rule would be silently wrong half the
    time, showing a number from the source the editor thought they had replaced."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="not both"):
        _dev(battery="/sys/class/power_supply/battery/capacity",
             battery_cmd="termux-api BatteryStatus")

    # Either alone is fine.
    assert _dev(battery="/sys/x").battery_cmd is None
    assert _dev(battery_cmd="termux-api BatteryStatus").battery is None


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
    probe.adopt_battery("kobo", "64\n")
    assert probe.battery("kobo").percent == 64
    assert probe.battery("kobo").error is None

    probe.adopt_battery("kobo", "cat: /sys/nope: No such file or directory\n")
    later = probe.battery("kobo")
    assert later.percent == 64, "one bad read threw away a good figure"
    assert "No such file" in later.error
    assert later.checked_at is not None


async def test_a_json_payload_without_a_charge_key_says_which_keys_it_saw(app):
    """The last line of a failed JSON read is `}`, which says nothing. Naming the keys is
    what turns "the bar is empty" into "you pointed it at the wrong command"."""
    probe = app.state.lib.probe
    probe.adopt_battery("kobo", '{"temperature": 31.7, "current": -596}')
    error = probe.battery("kobo").error
    assert "no charge key" in error
    assert "temperature" in error and "current" in error
    assert "}" != error.strip()

    # A plain `cat` failure still quotes the device's own complaint, which is the useful
    # line there.
    probe.adopt_battery("kobo", "cat: /sys/nope: Permission denied\n")
    assert probe.battery("kobo").error == "cat: /sys/nope: Permission denied"


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
    assert device.battery is None and device.battery_cmd is None

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


# ------------------------------------------------------------ the test button --


def test_the_test_script_reads_the_battery_too():
    """Test already pays for an ssh and already reads df. Reading the charge on the same
    connection is free, and leaving it out meant pressing Test refreshed the storage cell
    while the battery beside it kept the previous poll's figure."""
    from libnodes.routes.devices import _test_script

    plain = _test_script(_dev())
    assert "# battery" not in plain          # nothing declared, nothing asked
    assert "# df" in plain and "# rsync" in plain and "# write" in plain

    filed = _test_script(_dev(battery="/sys/class/power_supply/battery/capacity"))
    assert "# battery" in filed
    assert "cat /sys/class/power_supply/battery/capacity" in filed
    assert "# rsync" in filed                 # the rest of the test is intact

    # shlex.quote leaves an ordinary path alone, so quoting only shows on one that
    # needs it -- which is the case that matters.
    spaced = _test_script(_dev(battery="/sys/odd name/capacity"))
    assert "cat '/sys/odd name/capacity'" in spaced

    cmd = "/data/data/com.termux/files/usr/libexec/termux-api BatteryStatus"
    ran = _test_script(_dev(battery_cmd=cmd))
    assert cmd in ran and f"'{cmd}'" not in ran


def test_a_battery_command_with_braces_does_not_break_the_script():
    """The script is assembled with str.format for the target, and a battery_cmd is
    free-form shell that may well contain braces. Formatting the whole string after
    joining would read `{print $1}` as a field name and raise."""
    from libnodes.routes.devices import _test_script

    script = _test_script(_dev(battery_cmd="awk '{print $1}' /sys/class/power/cap"))
    assert "{print $1}" in script
    assert "/sdcard/Books" in script          # the target still got substituted


async def test_pressing_test_refreshes_the_battery(client, app, monkeypatch):
    """The row rides out of band, so it must carry both readings the test just took."""
    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"]
    object.__setattr__(device, "battery", "/sys/class/power_supply/battery/capacity")
    lib.probe._slot("kobo").battery = Battery(percent=12, checked_at=time.time() - 600)

    transcript = (
        "# df\n"
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/mmcblk0p3 31457280 10485760 20971520 33% /mnt/onboard\n"
        "# battery\n77\n"
        "# rsync\nrsync  version 3.2.7  protocol version 31\n"
        "# write\nwritable\n"
    )

    class _Proc:
        returncode = 0

        async def communicate(self):
            return (transcript.encode(), b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: _wrap(_Proc()))

    r = await client.post("/device/kobo/test")
    assert r.status_code == 200
    assert lib.probe.battery("kobo").percent == 77, "Test discarded the charge it read"
    assert "77%" in r.text
    # The verdict names it, parsed rather than echoed.
    assert "battery 77%" in r.text


async def _wrap(proc):
    return proc


# ------------------------------------------------------- a devices.yaml edit --


async def test_editing_devices_yaml_drops_every_cached_reading(app):
    """The config hot-reloads, the readings do not. Adding a `battery:` line changed what
    we would ask for while the flat 5-minute df cache kept us from asking — so the new
    column sat empty with nothing on the page to say why.

    Note this is `freespace_interval`, not the reachability backoff. Both default to 300s
    and they are unrelated.
    """
    from libnodes.probe import FreeSpace, Reachability

    probe = app.state.lib.probe
    now = time.time()
    for did in ("kobo", "phone"):
        probe._slot(did).reach = Reachability(state="online", last_ok=now,
                                              checked_at=now, next_probe_at=now + 3600)
        probe._slot(did).space = FreeSpace(total=100, used=50, free=50, checked_at=now)
        assert not probe._space_stale(did)

    probe.refresh_all()

    for did in ("kobo", "phone"):
        assert probe._space_stale(did), f"{did} kept a reading across a config edit"
        # The figure itself survives until the new one lands, so the cell does not blink
        # empty for a tick.
        assert probe.space(did).used == 50


async def _until(predicate, tries: int = 60, step: float = 0.02) -> bool:
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


async def test_a_config_edit_reaches_the_probe(app, monkeypatch):
    """End to end through the watcher subscription, not just the method in isolation."""
    lib = app.state.lib
    called = []
    monkeypatch.setattr(lib.probe, "refresh_all", lambda: called.append(True))

    async with app.router.lifespan_context(app):
        # The loop subscribes when it first runs, so an emit before that is nobody's to
        # receive. Real saves arrive long after startup; the test has to wait for it.
        assert await _until(lambda: lib.config_watch._subs), "loop never subscribed"

        # What the inotify watcher emits when devices.yaml is saved.
        lib.config_watch._emit()
        assert await _until(lambda: called), "a devices.yaml save did not refresh"


async def test_the_config_watcher_survives_a_bad_edit(app, monkeypatch):
    """A devices.yaml that cannot be parsed must not kill the task that watches it, or
    the next *good* save is never noticed either."""
    lib = app.state.lib
    calls = []

    def boom():
        calls.append(True)
        raise ValueError("unparseable")

    monkeypatch.setattr(lib.probe, "refresh_all", boom)

    async with app.router.lifespan_context(app):
        assert await _until(lambda: lib.config_watch._subs)
        lib.config_watch._emit()
        assert await _until(lambda: calls)

        # Still listening, so the next good save is still noticed.
        task = lib._config_reload_task
        assert task is not None and not task.done(), "the watcher died on a bad edit"
        lib.config_watch._emit()
        assert await _until(lambda: len(calls) >= 2), "it stopped after one failure"


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
