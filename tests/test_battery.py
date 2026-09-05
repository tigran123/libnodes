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

from libnodes.probe import (
    Battery,
    _parse_battery,
    _parse_power,
    _readings_script,
    _section,
    charging_command,
)

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


# ------------------------------------------------------------- the charger --


#: What s4l printed with its charger in, verbatim. The row this drives is amber.
TERMUX_CHARGING = """\
{
  "present": true,
  "technology": "Li-ion",
  "health": "GOOD",
  "plugged": "PLUGGED_AC",
  "status": "CHARGING",
  "temperature": 29.5,
  "voltage": 3868,
  "current": 470000,
  "current_average": 476,
  "percentage": 27,
  "level": 27,
  "scale": 100,
  "charge_counter": 7300000
}
"""


@pytest.mark.parametrize(
    "text,expected",
    [
        # --- a sysfs `status` file: one word, from the kernel's fixed set ---
        ("Charging\n", "charging"),
        ("Discharging\n", "unplugged"),
        # Both mean "attached but not taking" -- Full is done, Not charging is paused,
        # usually on temperature -- and neither is reachable off a charger.
        ("Full\n", "plugged"),
        ("Not charging\n", "plugged"),
        ("  charging  \n", "charging"),          # matched case-insensitively
        ("NOT_CHARGING\n", "plugged"),           # the upper snake case Android uses
        # Unknown means the driver does not know, which is not a fact about the charger.
        ("Unknown\n", None),
        ("", None),
        ("\n\n", None),
        # A failed cat names a path, and that path contains the word. Whole-line matching
        # is what keeps "no such file" from being read as "on the charger".
        ("cat: /sys/class/power_supply/battery/status: No such file or directory", None),
        ("cat: /sys/charging/status: Permission denied", None),
        ("100\n", None),                          # the capacity file, misrouted
        # --- termux-api JSON, where plugged and status are both present ---
        (TERMUX_CHARGING, "charging"),
        (TERMUX_JSON, "unplugged"),
        # plugged is the authority on attachment, status only on which bolt. A charger
        # that has stopped is still a charger.
        ('{"plugged": "PLUGGED_USB", "status": "FULL"}', "plugged"),
        ('{"plugged": "PLUGGED_WIRELESS", "status": "NOT_CHARGING"}', "plugged"),
        ('{"plugged": "PLUGGED_AC", "status": "UNKNOWN"}', "plugged"),
        ('{"plugged": "PLUGGED_AC"}', "plugged"),
        # ...and UNPLUGGED settles it whatever status claims.
        ('{"plugged": "UNPLUGGED", "status": "FULL"}', "unplugged"),
        # Only one of the two? Fall back to it alone.
        ('{"status": "CHARGING"}', "charging"),
        ('{"status": "DISCHARGING"}', "unplugged"),
        ('{"Plugged": "PLUGGED_AC", "Status": "CHARGING"}', "charging"),
        # Nothing that means a charger -- never guessed at. `current` is positive while
        # charging and negative while not, which is exactly what a looser parser would
        # reach for and exactly what varies by vendor.
        ('{"percentage": 46, "current": 470000}', None),
        ('{"plugged": "SOMETHING_NEW"}', None),
        ('{"status": "Unknown"}', None),
        ("[\"Charging\"]", None),                  # JSON, but not an object
        ("{ not json at all", None),
    ],
)
def test_parse_power(text, expected):
    assert _parse_power(text) == expected


def test_the_status_file_is_the_sibling_of_the_capacity_file():
    """Derived, not declared: sysfs keeps both names in the one supply directory, and a
    status file that is not there fails the cat and draws nothing -- a blank, not the
    confident wrong number that made `battery` a path in the first place."""
    assert charging_command(_dev(battery="/sys/class/power_supply/battery/capacity")) == (
        "cat /sys/class/power_supply/battery/status 2>&1"
    )
    # nexus10's vendor node name is nothing like the others; the derivation does not care.
    assert "/sys/class/power_supply/ds2784-fuelgauge/status" in charging_command(
        _dev(battery="/sys/class/power_supply/ds2784-fuelgauge/capacity")
    )

    # Quoted the way the capacity path is, or a directory with a space becomes two words.
    assert "'/sys/odd name/status'" in charging_command(_dev(battery="/sys/odd name/capacity"))

    # A bare filename names no supply directory, so there is no sibling to derive.
    assert charging_command(_dev(battery="capacity")) is None
    # And a battery_cmd node is never asked twice: its own JSON carries plugged/status.
    assert charging_command(_dev(battery_cmd="termux-api BatteryStatus")) is None
    assert charging_command(_dev()) is None


def test_a_device_can_say_where_its_charger_lives():
    """The sibling derivation is right for a device whose charge and charger are the same
    supply. The Nexus 10 is not one: its charge comes from `ds2784-fuelgauge`, which
    exposes no `status`, while the charger is `smb347-battery` two directories away, among
    five supplies with no rule relating them. That case is declared, not worked out."""
    nexus = _dev(
        battery="/sys/class/power_supply/ds2784-fuelgauge/capacity",
        charging="/sys/class/power_supply/smb347-battery/status",
    )
    assert charging_command(nexus) == (
        "cat /sys/class/power_supply/smb347-battery/status 2>&1"
    )
    # The override wins outright -- the fuel gauge's own (absent) status is never asked
    # for, or the transcript would carry two answers and a "No such file" beside them.
    script = _readings_script(nexus, "df /sdcard")
    assert "ds2784-fuelgauge/status" not in script
    assert "ds2784-fuelgauge/capacity" in script     # the charge still comes from there
    assert script.count("# power") == 1

    # Quoted like any other path.
    assert "'/sys/odd name/status'" in charging_command(
        _dev(battery="/sys/x/capacity", charging="/sys/odd name/status")
    )

    # A charger source with no charge source is read by nobody, so it fails loudly rather
    # than leaving someone staring at a line they wrote and a column that never fills.
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="needs a battery"):
        _dev(charging="/sys/class/power_supply/smb347-battery/status")


def test_the_current_sign_is_not_a_charging_signal():
    """It looks like the portable answer and it is the opposite of one. Measured across
    this fleet: the Nexus 10 reports POWER_SUPPLY_CURRENT_NOW positive while charging and
    negative once unplugged, while lg and bk both report it *positive* with
    POWER_SUPPLY_STATUS=Discharging -- the same number meaning opposite things on three
    devices. A `status` file says which; a sign says which vendor."""
    assert _parse_power("POWER_SUPPLY_CURRENT_NOW=249218") is None
    assert _parse_power("POWER_SUPPLY_CURRENT_NOW=-540781") is None
    assert _parse_power("249218") is None
    assert _parse_power("-540781") is None
    # ...and the same numbers inside the JSON a termux node prints.
    assert _parse_power('{"current": 470000, "current_average": 476}') is None


def test_the_charger_rides_along_on_the_same_ssh():
    """Still one connection. The charger is a second `cat` in the same script, not a
    second probe -- on a sleeping Termux node the connection is the whole cost."""
    script = _readings_script(
        _dev(battery="/sys/class/power_supply/battery/capacity"), "df -Pk /sdcard/Books"
    )
    assert script.count("df -Pk") == 1
    assert "# df" in script and "# battery" in script and "# power" in script
    assert "battery/capacity" in script and "battery/status" in script

    # A termux node gets no second command at all -- one invocation answers both.
    cmd = "/data/data/com.termux/files/usr/libexec/termux-api BatteryStatus"
    termux = _readings_script(_dev(battery_cmd=cmd), "df /sdcard/Books")
    assert termux.count("BatteryStatus") == 1
    assert "# power" not in termux

    # And a node with no battery still gets the bare df it always got.
    assert _readings_script(_dev(), "df -Pk /sdcard/Books") == "df -Pk /sdcard/Books"


def test_the_power_section_does_not_swallow_the_battery():
    """`# power` follows `# battery`, so the marker that ends one begins the other. Get
    that wrong and the charge parses out of a status word or the reverse."""
    out = "# df\nx 100 50 50 50% /\n# battery\n64\n# power\nCharging\n"
    assert _section(out, "battery").strip() == "64"
    assert _section(out, "power").strip() == "Charging"
    assert _parse_battery(_section(out, "battery")) == 64
    assert _parse_power(_section(out, "power")) == "charging"


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


async def test_a_charge_state_that_stops_reading_blanks_the_bolt(app):
    """The percentage carries forward across a bad read and the bolt does not, on purpose.
    A level moves slowly, so a minute-old one is still roughly true; a bolt is a claim
    about *now*, and a stale one says a device is on a charger it may have been unplugged
    from since. Not read this time means no bolt."""
    probe = app.state.lib.probe
    probe.adopt_battery("kobo", "64\n", "Charging\n")
    assert probe.battery("kobo").percent == 64
    assert probe.battery("kobo").power == "charging"

    # The capacity still reads, the status file has gone.
    probe.adopt_battery("kobo", "70\n", "cat: /sys/x/status: No such file or directory")
    assert probe.battery("kobo").percent == 70
    assert probe.battery("kobo").power is None, "a stale bolt outlived its reading"

    # And the other way round: the capacity fails, the charger still answers.
    probe.adopt_battery("kobo", "cat: /sys/x/capacity: No such file", "Full\n")
    assert probe.battery("kobo").percent == 70, "one bad read threw away a good figure"
    assert probe.battery("kobo").power == "plugged"


async def test_a_termux_node_reads_its_charger_out_of_the_battery_payload(app):
    """No `# power` section for a battery_cmd node, so the fallback to the battery text is
    what makes the bolt work there at all. It has to key on the section being *empty* and
    not on the parse failing -- a status file that errored is not empty, and must not send
    us looking for a charger inside a bare integer."""
    probe = app.state.lib.probe
    probe.adopt_battery("kobo", TERMUX_CHARGING, "")
    assert probe.battery("kobo").percent == 27
    assert probe.battery("kobo").power == "charging"

    probe.adopt_battery("kobo", TERMUX_JSON, "")
    assert probe.battery("kobo").power == "unplugged"


async def test_a_full_battery_is_not_the_same_bolt_as_a_charging_one(client, app):
    """Two states, two colours: amber says the figure beside it is climbing, green says
    the device is on a charger and done. Unplugged draws nothing, and neither does a
    charger we could not read -- which is why the two are kept apart in the record even
    though they render alike."""
    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"]
    object.__setattr__(device, "battery", "/sys/class/power_supply/battery/capacity")
    now = time.time()

    async def cell(power):
        lib.probe._slot("kobo").battery = Battery(percent=64, power=power, checked_at=now)
        row = (await client.get("/devices/rows")).text
        return row.split('data-label="Battery"')[1].split('data-label="Last seen"')[0]

    charging = await cell("charging")
    assert "bolt-charging" in charging
    assert "bolt-plugged" not in charging
    assert "64%" in charging

    plugged = await cell("plugged")
    assert "bolt-plugged" in plugged
    assert "bolt-charging" not in plugged

    for quiet in ("unplugged", None):
        assert "bolt" not in await cell(quiet), f"{quiet} drew a bolt"

    # The tints the markup asks for have to exist, and have to differ -- one rule missing
    # and both states render in the inherited colour, saying the same thing twice.
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()
    tints = {
        name: css.split(f".{name} {{")[1].split("}")[0].strip()
        for name in ("bolt-charging", "bolt-plugged")
    }
    assert all(tints.values()), tints
    assert tints["bolt-charging"] != tints["bolt-plugged"]
    # --warn-fill, not --warn: that token exists because plain amber reads as red at
    # small sizes, and 8px of glyph is exactly that size.
    assert "--warn-fill" in tints["bolt-charging"]
    assert "--ok" in tints["bolt-plugged"]


async def test_both_views_draw_the_same_bolt(client, app):
    """One include, so a bolt cannot come to mean different things in TABLE and GRID."""
    from libnodes.templating import TEMPLATES_DIR

    # device_card.html, not device_grid.html: the card body moved out of the grid's loop
    # into its own fragment so /device/{id}/card can answer a Retry with one card.
    for name in ("device_row.html", "device_card.html"):
        text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        assert '{% include "charging_bolt.html" %}' in text, name
        assert "<svg" not in text, f"{name} drew its own bolt instead of including it"

    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"]
    object.__setattr__(device, "battery", "/sys/class/power_supply/battery/capacity")
    lib.probe._slot("kobo").battery = Battery(
        percent=27, power="charging", checked_at=time.time()
    )
    assert "bolt-charging" in (await client.get("/devices/rows")).text
    assert "bolt-charging" in (await client.get("/devices/grid")).text


async def test_the_tooltip_tells_the_two_quiet_states_apart(client, app):
    """The bolt cannot distinguish "on its own battery" from "we could not read the
    charger", because both draw nothing. The tooltip is where that distinction lives."""
    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"]
    object.__setattr__(device, "battery", "/sys/class/power_supply/battery/capacity")
    now = time.time()

    async def note(power):
        lib.probe._slot("kobo").battery = Battery(percent=64, power=power, checked_at=now)
        row = (await client.get("/devices/rows")).text
        return row.split('data-label="Battery"')[1].split('data-label="Last seen"')[0]

    assert "on battery" in await note("unplugged")
    assert "on battery" not in await note(None)
    assert "charging" in await note("charging")
    assert "on charger, not charging" in await note("plugged")


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


def test_the_test_script_reads_the_charger_too():
    """Test already pays for the ssh and already reads the capacity; the sibling status is
    one more `cat` on the same connection. Routed through `charging_command` so pressing
    Test cannot read a different set of things than the poll does."""
    from libnodes.routes.devices import _test_script

    filed = _test_script(_dev(battery="/sys/class/power_supply/battery/capacity"))
    assert "# power" in filed
    assert "cat /sys/class/power_supply/battery/status" in filed
    assert "# battery" in filed and "# rsync" in filed and "# write" in filed

    # A termux node adds nothing: one invocation already answered both.
    cmd = "/data/data/com.termux/files/usr/libexec/termux-api BatteryStatus"
    assert "# power" not in _test_script(_dev(battery_cmd=cmd))
    assert "# power" not in _test_script(_dev())


async def test_pressing_test_reports_the_charger(client, app, monkeypatch):
    """The verdict names it, parsed rather than echoed, so what the dialog claims is the
    state the row beside it draws."""
    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"]
    object.__setattr__(device, "battery", "/sys/class/power_supply/battery/capacity")

    transcript = (
        "# df\n"
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/mmcblk0p3 31457280 10485760 20971520 33% /mnt/onboard\n"
        "# battery\n27\n"
        "# power\nCharging\n"
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
    assert lib.probe.battery("kobo").power == "charging", "Test discarded what it read"
    assert "battery 27% (charging)" in r.text
    assert "None" not in r.text.split("battery 27%")[1][:40], (
        "the verdict printed a bare None for a state with no wording"
    )
    # ...and the row it swaps out of band carries the bolt the verdict just described.
    assert "bolt-charging" in r.text


async def test_an_unplugged_verdict_says_nothing_rather_than_None(client, app, monkeypatch):
    """`_POWER_VERDICT` has no wording for "unplugged" because there is nothing to add to
    "battery 100%". Looking that up without a default put the string "None" on the end of
    the one line the dialog exists to print."""
    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"]
    object.__setattr__(device, "battery", "/sys/class/power_supply/battery/capacity")

    transcript = (
        "# df\nFilesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/mmcblk0p3 31457280 10485760 20971520 33% /mnt/onboard\n"
        "# battery\n100\n"
        "# power\nDischarging\n"
        "# rsync\nrsync  version 3.2.7  protocol version 31\n"
        "# write\nwritable\n"
    )

    class _Proc:
        returncode = 0

        async def communicate(self):
            return (transcript.encode(), b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: _wrap(_Proc()))

    r = await client.post("/device/kobo/test")
    assert "battery 100%" in r.text
    assert "battery 100%None" not in r.text
    assert "100%None" not in r.text

    # A charger source that could not be read is the same shape of lookup miss.
    lib.probe.adopt_battery("kobo", "100\n", "cat: /sys/nope: No such file or directory")
    assert lib.probe.battery("kobo").power is None


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


# --------------------------------------------------------------- last seen --


async def test_the_row_dates_the_readings_it_shows(client, app):
    """STORAGE and BATTERY are up to `freespace_interval` (300s) old while the dot beside
    them is re-probed every 10-30s and the row is re-rendered every 10s. Without a column
    for it the page asserts a measurement it did not just take — the same fault the amber
    dot's backoff had."""
    from libnodes.probe import FreeSpace, Reachability

    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"]
    object.__setattr__(device, "battery", "/sys/class/power_supply/battery/capacity")

    now = time.time()
    lib.probe._slot("kobo").reach = Reachability(
        state="online", last_ok=now, checked_at=now
    )
    lib.probe._slot("kobo").space = FreeSpace(
        total=32212254720, used=5368709120, free=26843545600, checked_at=now - 180
    )
    lib.probe._slot("kobo").battery = Battery(percent=64, checked_at=now - 180)

    row = (await client.get("/devices/rows")).text
    cell = row.split('data-label="Last seen"')[1].split("</div>")[0]
    printed = cell.split(">", 1)[1]          # past the tag, which holds the tooltip
    # The reading's age, not the connect's: the two are three minutes apart here, and
    # printing the fresher one would date the storage beside it wrongly.
    assert "3m ago" in printed, printed
    assert "0s ago" not in printed
    assert "answered 0s ago" in cell, "the tooltip drops the reachability comparison"
    assert "df + battery read at" in cell

    head = (await client.get("/devices")).text
    assert "<div>Last seen</div>" in head


async def test_a_node_never_read_says_so_rather_than_guessing(client, app):
    row = (await client.get("/devices/rows")).text
    cell = row.split('data-label="Last seen"')[1].split("</div>")[0]
    assert "never" in cell
    assert "no reading yet" in cell


async def test_an_offline_row_still_shows_the_figures_it_has(client, app):
    """The `—` an offline row printed was hiding a reading it still held — while the card
    view and every *sleeping* row printed theirs. LAST SEEN is what makes showing it
    honest: faint text, a faint bar, and the age beside it."""
    from libnodes.probe import FreeSpace, Reachability

    lib = app.state.lib
    now = time.time()
    lib.probe._slot("kobo").reach = Reachability(
        state="offline", last_ok=now - 14400, checked_at=now, error="timed out"
    )
    lib.probe._slot("kobo").space = FreeSpace(
        total=32212254720, used=5368709120, free=26843545600, checked_at=now - 14400
    )

    row = (await client.get("/devices/rows")).text
    assert "is-offline" in row
    storage = row.split('data-label="Storage"')[1].split('data-label="Battery"')[0]
    assert "5.0G / 30.0G" in storage, storage
    assert "track-disk" in storage, "the bar has no modifier to dim it by"
    assert "4h ago" in row.split('data-label="Last seen"')[1].split("</div>")[0]

    # The dimming is a rule, not an inline style, so it has to exist.
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()
    assert ".trow.is-offline .track-disk > i" in css


async def test_a_low_battery_keeps_its_tint_on_an_offline_row(client, app):
    """The faint fill is scoped to the storage bar for a reason: an unscoped
    `.trow.is-offline .track > i` outranks `.track-err > i`, and a flat battery is the
    likeliest explanation for a red dot — the one reading that must stay loud."""
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()
    assert ".trow.is-offline .track > i" not in css

    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"]
    object.__setattr__(device, "battery", "/sys/class/power_supply/battery/capacity")
    from libnodes.probe import Reachability

    now = time.time()
    lib.probe._slot("kobo").reach = Reachability(
        state="offline", last_ok=now - 7200, checked_at=now, error="timed out"
    )
    lib.probe._slot("kobo").battery = Battery(percent=4, checked_at=now - 7200)

    row = (await client.get("/devices/rows")).text
    battery = row.split('data-label="Battery"')[1].split('data-label="Last seen"')[0]
    assert "track-err" in battery
    assert "track-disk" not in battery


async def test_both_views_report_one_age(client, app):
    """A card and a row showing the same figures must agree about how old they are."""
    from libnodes.probe import FreeSpace, Reachability

    lib = app.state.lib
    now = time.time()
    for did in ("kobo", "phone"):
        lib.probe._slot(did).reach = Reachability(
            state="online", last_ok=now, checked_at=now
        )
        lib.probe._slot(did).space = FreeSpace(
            total=100, used=50, free=50, checked_at=now - 7200
        )

    assert "2h ago" in (await client.get("/devices/rows")).text
    cards = (await client.get("/devices/grid")).text
    assert "2h ago" in cards
    assert ">seen<" in cards


# ------------------------------------------------------------- the layout --


def test_the_test_dialog_fits_a_whole_termux_payload():
    """`termux-api BatteryStatus` prints 15 lines of JSON, inside a 24-line transcript --
    measured on s4l. The pane was capped at 190px and top-anchored to the *end*, so the
    head of that JSON was not merely scrolled off, it was past the start edge of an
    overflowing flex column where scrolling cannot reach it. Computed from the stylesheet
    rather than written down, so shrinking either number fails here."""
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()

    # Anchored to the line start: `:root[data-theme="light"] .term {` contains the same
    # substring and comes first in the file.
    term = css.split("\n.term {")[1].split("}")[0]
    font = float(re.search(r"font-size:\s*([\d.]+)px", term).group(1))
    leading = float(re.search(r"line-height:\s*([\d.]+)", term).group(1))
    pad_y = float(re.search(r"padding:\s*([\d.]+)px", term).group(1))

    doc = css.split("\n.term-doc {")[1].split("}")[0]
    cap = float(re.search(r"max-height:\s*min\(([\d.]+)px", doc).group(1))

    lines = (cap - 2 * pad_y) / (font * leading)
    assert lines >= 24, (
        f"{cap}px shows {lines:.1f} lines; a termux transcript is 24 and would clip"
    )

    # ...and it has to read from the top, or the cap above buys nothing.
    assert "flex-start" in doc
    # The live dock log keeps the tail — that one *is* a stream, and the newest line is
    # the one you want. The two must not be collapsed into one rule.
    assert "flex-end" in term

    strip = (ROOT / "libnodes" / "templates" / "fragments" / "test_strip.html").read_text()
    assert 'class="term term-doc"' in strip
    assert "max-height" not in strip, "the geometry drifted back into an inline style"


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


def test_the_stack_breakpoint_clears_the_row_floor():
    """A column adds a floor, and the row has to fit above the width where it stops
    stacking — or there is a band in between where it has neither layout.

    The floors summed to 954px against a 1200px breakpoint, which fitted with 20px to
    spare; LAST SEEN took them to 1010px, needing 1236px of viewport, and opened a 36px
    band where the grid was over-constrained and paid for it by wrapping the Actions
    buttons. Computed from the stylesheet rather than written down, so the next column
    fails here instead of on somebody's screen.
    """
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()

    block = css.split(".device-grid {")[1].split("}")[0]
    floor = sum(int(n) for n in re.findall(r"minmax\((\d+)px", block))
    rail = int(re.search(r"--rail:\s*(\d+)px", css).group(1))
    gutter = int(re.search(r"--gutter:\s*(\d+)px", css).group(1))
    stack = int(
        re.search(r"@media \(max-width: (\d+)px\) \{\s*\.thead\.device-grid", css).group(1)
    )

    needed = floor + rail + 2 * gutter
    assert stack >= needed, (
        f"{floor}px of track floors need {needed}px of viewport, but the row stops "
        f"stacking at {stack}px — leaving {needed - stack}px with no working layout"
    )


def test_the_file_grid_stacks_before_it_runs_out_of_panel():
    """The Library table is the navigator now, so the width at which it stops being a
    table is a navigation decision rather than a cosmetic one.

    Its arithmetic has two regimes, which the device row's does not: at 972px the rail
    goes position:fixed and stops taking 190px out of the flow, so the *narrower* viewport
    is the roomier one. It also has to account for --scale, because a media query is
    matched against the unzoomed viewport while every length inside the layout is zoomed.
    """
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()

    block = css.split(".file-grid {")[1].split("}")[0]
    # The 26px checkbox track carries no minmax: it holds an 11px box and has nothing
    # to give.
    fixed = int(re.search(r"grid-template-columns:\s*\n?\s*(\d+)px", block).group(1))
    floor = fixed + sum(int(n) for n in re.findall(r"minmax\((\d+)px", block))

    rail = int(re.search(r"--rail:\s*(\d+)px", css).group(1))
    gutter = int(re.search(r"--gutter:\s*(\d+)px", css).group(1))
    scale = float(re.search(r"--scale:\s*([\d.]+)", css).group(1))

    stack = int(
        re.search(r"@media \(max-width: (\d+)px\) \{\s*\.thead\.file-grid", css).group(1)
    )
    # The block that takes the rail out of the flow; `body` is its first selector.
    rail_floats = int(
        re.search(r"@media \(max-width: (\d+)px\) \{\s*body", css).group(1)
    )

    if stack <= rail_floats:
        # No rail in the flow, and that same block narrows .lib-body's padding to 12px.
        needed = (floor + 2 * 12) * scale
    else:
        needed = (floor + rail + 2 * gutter) * scale

    assert stack >= needed, (
        f"{floor}px of track floors need {needed:.0f}px of viewport, but the row stops "
        f"stacking at {stack}px — leaving {needed - stack:.0f}px with no working layout"
    )

    # A Nexus 10 in landscape is 1280 CSS px and used to get the stacked cards, because
    # the breakpoint was set when a 300px tree stood beside this table.
    assert stack < 1280, f"1280px landscape still gets cards at a {stack}px stack"


# ----------------------------------------------------- the tablet's zoom --


def _media_block(css: str, needle: str) -> str:
    """The body of the first @media block whose query contains `needle`."""
    open_brace = css.index("{", css.index(needle))
    depth, i = 1, open_brace + 1
    while depth:
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
        i += 1
    return css[open_brace + 1 : i - 1]


def _rule(block: str, selector: str) -> str:
    """One rule's declarations, whitespace-normalised, for comparing two copies."""
    body = block.split(selector + " {")[1].split("}")[0]
    return " ".join(body.split())


def test_the_tablet_band_hides_the_rail_the_way_the_narrow_one_does():
    """The tablet band repeats the 972px block's rail rules instead of widening its query,
    because only some of that block belongs to a tablet -- it wants two card columns where
    a phone wants one. A repeat can drift, so it is pinned: an off-canvas rail that opens
    in one regime and not the other is a nav that half works, and nothing else would say so.
    """
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()

    narrow = _media_block(css, "@media (max-width: 972px) {\n  body")
    tablet = _media_block(css, "@media (min-width: 973px) and (max-width: 1280px)")

    for selector in (".rail", ".shell.rail-open .rail", ".rail-toggle"):
        assert _rule(narrow, selector) == _rule(tablet, selector), selector

    # The half that must differ, and the reason the query was not simply widened.
    assert _rule(narrow, ".cards") == "grid-template-columns: 1fr;"
    assert _rule(tablet, ".cards") == "grid-template-columns: repeat(2, 1fr);"


def test_the_tablet_zoom_leaves_the_library_a_table():
    """The file table is the only navigator, so the zoom must not over-constrain it.

    The tablet band is above 972px, where the file grid has not stacked -- so its seven
    track floors have to fit a panel that the zoom has made narrower. They do, and the
    margin is what this checks: at the band's narrow end the rail is already out of the
    flow, so the panel is `viewport / --scale - 2x12` of .lib-body padding.
    """
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()

    block = css.split(".file-grid {")[1].split("}")[0]
    fixed = int(re.search(r"grid-template-columns:\s*\n?\s*(\d+)px", block).group(1))
    floor = fixed + sum(int(n) for n in re.findall(r"minmax\((\d+)px", block))

    tablet = _media_block(css, "@media (max-width: 1280px) and (hover: none)")
    scale = float(re.search(r"--scale:\s*([\d.]+)", tablet).group(1))
    assert scale > float(re.search(r"--scale:\s*([\d.]+)", css).group(1)), (
        "the tablet block no longer zooms anything"
    )

    needed = (floor + 2 * 12) * scale
    assert needed <= 973, (
        f"{floor}px of track floors need {needed:.0f}px at {scale}x zoom, but the tablet "
        f"band starts at 973px — the Library would have no working layout on a tablet"
    )


def test_the_tablet_band_stops_where_the_device_row_stops_stacking():
    """The zoom's ceiling is not a taste: above it the device row is a single line again,
    and its nine track floors (1022px) do not fit a panel the zoom has shrunk. Holding the
    two numbers equal is what keeps the band entirely inside the stacked regime.
    """
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()

    stack = int(
        re.search(r"@media \(max-width: (\d+)px\) \{\s*\.thead\.device-grid", css).group(1)
    )
    ceiling = int(
        re.search(r"@media \(max-width: (\d+)px\) and \(hover: none\)", css).group(1)
    )
    assert ceiling <= stack, (
        f"the tablet zooms up to {ceiling}px but the device row stops stacking at {stack}px"
    )


def test_the_stacked_row_pairs_its_cells():
    """The stacked device row is the fallback for every narrow screen, not a phone layout.

    Nine label/value lines per device is what a 27" 2.5K monitor turned portrait shows --
    1152 CSS px at 125%, well under the 1348px a single-line row needs -- and nine devices
    then go past a 2560px screen one at a time. Two-up halves it.

    The width it starts at is the row's own arithmetic, and the first attempt was not: 768
    was picked as a tidy tablet number, and a Galaxy Tab S4 turned out to report 700 CSS px
    where a Nexus 10 of identical size and resolution reports 800 -- Samsung's Screen zoom
    moves the density, so the breakpoint fell between two tablets of the same panel. So it is
    derived from the widest thing in the row that has no tooltip to fall back on: the address,
    124px at 11.5px mono. A column is that plus the label, the gap and the cell's side
    padding, and two of them plus .view's padding have to fit the narrowest regime that can
    reach this rule -- a touch screen under 972px, where the rail is already out of flow and
    --scale is 1.35.
    """
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()

    start = int(
        re.search(r"@media \(min-width: (\d+)px\) and \(max-width: 1280px\)", css).group(1)
    )
    two_up = _media_block(css, f"@media (min-width: {start}px) and (max-width: 1280px)")
    assert "grid-template-columns: 1fr 1fr;" in two_up

    # The Actions cell floors at 320px -- failure text plus three buttons -- so it takes the
    # whole row rather than half of one, exactly as .subrow does.
    assert "grid-column: 1 / -1;" in _rule(
        two_up, ".trow.device-grid .cell-actions,\n  .trow.jobs-grid .cell-actions"
    )

    stacked = _media_block(css, "@media (max-width: 1280px) {")
    gap = int(re.search(r"> \* \{[^}]*gap: (\d+)px", stacked, re.S).group(1))
    side = int(re.search(r"> \* \{[^}]*padding: \d+px (\d+)px", stacked, re.S).group(1))

    # Two-up narrows the label, because 92px was never its text: "LAST SEEN" is the widest at
    # 60.7 layout px, measured in the 9.5px mono these labels use. Anything under that wraps
    # the label onto a second line and grows every row.
    wide = int(re.search(r"::before \{[^}]*width: (\d+)px", stacked, re.S).group(1))
    label = int(re.search(r"::before,[^{]*\{\s*width: (\d+)px", two_up).group(1))
    assert 61 <= label <= wide, f"a {label}px label cannot hold LAST SEEN's 60.7px"

    scale = float(
        re.search(r"--scale:\s*([\d.]+)",
                  _media_block(css, "@media (max-width: 1280px) and (hover: none)")).group(1)
    )
    narrow = _media_block(css, "@media (max-width: 972px) {\n  body")
    pad = int(re.search(r"\.lib-body,\n  \.view \{\s*padding-left: (\d+)px", narrow).group(1))

    column = label + gap + 2 * side + 124
    panel = start / scale - 2 * pad
    assert panel >= 2 * column, (
        f"two {column}px columns need {2 * column:.0f}px, but a touch screen at the {start}px "
        f"breakpoint has {panel:.0f}px — the address would clip with nothing to recover it"
    )
    # ...and the tablet this was measured on has to be inside the band, not beside it.
    assert start <= 700, f"a Galaxy Tab S4 is 700 CSS px and the band starts at {start}"


def test_the_touch_minimum_survives_the_zoom():
    """44px is a physical rule -- 0.27" is what a thumb needs -- and the zoom multiplies it.

    A 10" tablet in portrait is 800 CSS px, so it lands in the 972px block written for a
    phone while --scale 1.35 is still on: 44 x 1.35 = 59.4 CSS px, 0.41" on a screen with
    143.5 CSS px to the inch. The touch block divides the zoom back out, and that this
    product still lands on 44 is the entire point of the change -- neither number means
    anything without the other, and nothing else would notice either one drifting.
    """
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()

    narrow = _media_block(css, "@media (max-width: 972px) {\n  body")
    touch = _media_block(css, "@media (max-width: 972px) and (hover: none)")
    scale = float(
        re.search(r"--scale:\s*([\d.]+)",
                  _media_block(css, "@media (max-width: 1280px) and (hover: none)")).group(1)
    )

    # var(--bar) elsewhere in the narrow block carries no px literal, so the first of these
    # is the touch-target rule in both blocks.
    base = int(re.search(r"min-height: (\d+)px", narrow).group(1))
    shrunk = int(re.search(r"min-height: (\d+)px", touch).group(1))

    assert abs(shrunk * scale - base) <= 1, (
        f"{shrunk}px x {scale} = {shrunk * scale:.1f}px, but the physical target is {base}px"
    )
    # The type is not what was oversized: it is already smaller here than on a desktop.
    assert "font-size" not in touch, "the touch block started resizing text"


def test_a_tablet_in_portrait_fits_two_cards():
    """One card column across a 10" tablet was half the screen empty.

    The floor is the card's own, measured against the running service by forcing the columns
    narrower and narrower: content first overflows the box at 210px and the Test/Retry/Actions
    strip at 200px, so 220 is the floor and the declared value keeps a margin over it. The
    first attempt borrowed 256px from the desktop's narrowest three-up card instead, which
    needed 736 real px for a second column -- and a Galaxy Tab S4 is 700 CSS px, not the 800
    a Nexus 10 of the same size and resolution reports, so one tablet got two columns and the
    other stayed at one. Both directions matter: a bigger floor loses the S4's second column,
    a smaller one gives a phone two cards of nothing.
    """
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()

    touch = _media_block(css, "@media (max-width: 972px) and (hover: none)")
    floor = int(re.search(r"\.cards \{[^}]*minmax\((\d+)px", touch, re.S).group(1))
    gap = int(re.search(r"^\.cards \{[^}]*gap: (\d+)px", css, re.S | re.M).group(1))
    scale = float(
        re.search(r"--scale:\s*([\d.]+)",
                  _media_block(css, "@media (max-width: 1280px) and (hover: none)")).group(1)
    )
    narrow = _media_block(css, "@media (max-width: 972px) {\n  body")
    pad = int(re.search(r"\.lib-body,\n  \.view \{\s*padding-left: (\d+)px", narrow).group(1))

    assert floor >= 220, f"a card's content overflows its box below 220px, and this is {floor}"

    def columns(viewport: float) -> int:
        room = viewport / scale - 2 * pad
        return int((room + gap) // (floor + gap))

    assert columns(700) == 2, "a Galaxy Tab S4 in portrait is 700 CSS px and wants two"
    assert columns(800) == 2, "a Nexus 10 in portrait is 800 CSS px and wants two"
    assert columns(412) == 1, "a phone must not be given two columns of 130px"


def test_the_size_and_date_columns_cannot_wrap():
    """SIZE and MODIFIED hold a formatted string of known width, so their track floors are
    that string rather than a guess.

    Every other column holds something that can shrink or elide; these two hold text that
    can only break. At the old 64/84 floors both fitted at their 84/108 maxima and neither
    fitted once the tracks were squeezed -- so the columns were correct on a desktop and
    broke at 980 CSS px, the width Chrome's "Desktop site" hands a Nexus 10.

    The widths are measured, not derived: "136.1 MB" is 55px and "2026-05-20" is 69px in
    the 11.5px JetBrains Mono these cells use. The font is self-hosted (static/fonts), so
    that advance is the same on every device the fleet has.
    """
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()

    block = css.split(".file-grid {")[1].split("}")[0]
    floors = [int(n) for n in re.findall(r"minmax\((\d+)px", block)]
    # NAME, FORMAT, SIZE, MODIFIED, PRESENT ON, PUSH -- the 26px checkbox carries no minmax.
    _, _, size, modified, _, _ = floors

    padding = css.split(".file-grid > * {")[1].split("}")[0]
    side = int(re.search(r"padding:\s*\d+px\s+(\d+)px", padding).group(1))

    for name, floor, text in (("SIZE", size, 55), ("MODIFIED", modified, 69)):
        needed = text + 2 * side
        assert floor >= needed, (
            f"{name}'s widest value needs {needed}px but the track floors at {floor}px — "
            f"it will break onto a second line wherever the grid is squeezed"
        )


def test_the_stylesheet_switches_off_chromes_text_autosizer():
    """Chrome on Android inflates the text of blocks it takes for a page's main column,
    per block rather than per page -- so the Library's SIZE and MODIFIED came out half
    again the size of the NAME beside them and wrapped, while the desktop stayed perfect.

    --scale is how this app decides how big it should be; the autosizer is a second opinion
    on top of it. There is no way to see this one from here: it needs the device, which is
    exactly why it is pinned rather than left to be noticed again.
    """
    css = (ROOT / "libnodes" / "static" / "app.css").read_text()

    root = css.split("html {")[1].split("}")[0]
    # The lookbehind matters: the prefixed name *contains* the unprefixed one, so a plain
    # substring test for the standard property passes on the vendored one alone.
    assert re.search(r"(?<![-\w])text-size-adjust:\s*100%", root), (
        "the standard property is missing"
    )
    assert re.search(r"-webkit-text-size-adjust:\s*100%", root), (
        "the prefixed property is missing, and it is the one Chrome honours"
    )


def test_the_card_offers_every_action_the_row_does():
    """TABLE and GRID are two renderings of one fleet, not two feature sets.

    The card was written without Test and stayed that way: the grid offered every action
    that *writes* -- Full Sync, Adopt, Scan, all behind Actions -- and withheld the only
    one that changes nothing. So a red node in GRID could be retried but not diagnosed
    without switching back to TABLE, which is the one moment the diagnosis is wanted.
    Compared by endpoint rather than by label, because that is what an action *is*.
    """
    templates = ROOT / "libnodes" / "templates"

    def endpoints(name: str) -> set[str]:
        text = (templates / name).read_text(encoding="utf-8")
        return set(re.findall(r'hx-(?:post|get)="([^"]+)"', text))

    row = endpoints("device_row.html")
    card = endpoints("device_card.html")

    assert row, "device_row.html has no actions at all — the regex has stopped matching"
    assert row - card == set(), f"the card cannot reach {sorted(row - card)}"
    assert card - row == set(), f"the row cannot reach {sorted(card - row)}"
