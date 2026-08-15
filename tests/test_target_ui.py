"""`target_ui`: a cosmetic alias for a long device path.

It must never leak into a command — that is the whole risk of having two of them.
"""

from __future__ import annotations

from libnodes.models import parse_devices

TERMUX = """
devices:
  - id: lg
    name: LG G4
    type: termux
    host: lg
    port: 2222
    user: root
    target: /data/data/com.termux/files/home/sd/Books
    target_ui: ~/sd/Books
  - id: plain
    name: Plain
    type: linux
    host: h
    target: /srv/books
"""


def test_display_target_prefers_the_alias():
    config, issues = parse_devices(TERMUX)
    assert issues == []
    assert config.by_id["lg"].display_target == "~/sd/Books"


def test_display_target_falls_back_to_the_real_path():
    config, _ = parse_devices(TERMUX)
    assert config.by_id["plain"].display_target == "/srv/books"


def test_alias_never_reaches_the_rsync_command(settings):
    """The alias is unusable as a path — `~` would not even expand here."""
    from libnodes.jobs import build_argv

    config, _ = parse_devices(TERMUX)
    device = config.by_id["lg"]
    argv = build_argv(device, config, ["Science"], settings)

    assert argv[-1] == "root@lg:/data/data/com.termux/files/home/sd/Books/"
    assert not any("~/sd/Books" in a for a in argv)


def test_alias_never_reaches_the_ssh_command(settings):
    from libnodes.probe import ssh_argv

    config, _ = parse_devices(TERMUX)
    argv = ssh_argv(config.by_id["lg"], settings)
    assert not any("~/sd" in a for a in argv)


async def test_device_row_shows_the_alias_and_keeps_the_real_path_as_a_tooltip(
    client, devices_file
):
    devices_file.write_text(TERMUX.lstrip())
    r = await client.get("/devices/rows")
    assert "~/sd/Books" in r.text
    assert 'title="/data/data/com.termux/files/home/sd/Books"' in r.text


async def test_picker_shows_the_alias(client, devices_file):
    devices_file.write_text(TERMUX.lstrip())
    r = await client.get("/jobs/picker", params={"path": "Science"})
    assert "~/sd/Books" in r.text


async def test_target_ui_is_optional(client, devices_file):
    """Existing configs without the field keep working unchanged."""
    r = await client.get("/devices/rows")
    assert "/mnt/onboard/Books" in r.text
