"""ssh keepalives, which exist because the Pi multiplexes.

`ssh_argv` and `build_argv` do not pass `-F none`, so the Pi's `~/.ssh/config` applies and
its `ControlMaster auto` / `ControlPersist 3600` gives every device one long-lived master
shared by the probe and the transfer. That is worth keeping — measured on the fleet, a
fresh handshake to a phone is 680-850ms against 140-355ms through a master — but a master
outlives the connection under it, so a device that sleeps leaves one wedged and the next
user gets `mux_client_request_session: read from master failed: Broken pipe`.
"""

from __future__ import annotations

from libnodes.jobs import build_argv
from libnodes.probe import (
    SERVER_ALIVE_COUNT_MAX,
    SERVER_ALIVE_INTERVAL,
    ssh_argv,
)


def _opts(argv: list[str]) -> dict[str, str]:
    """The -o pairs of an argv, as a dict."""
    out = {}
    for flag, value in zip(argv, argv[1:]):
        if flag == "-o" and "=" in value:
            key, _, val = value.partition("=")
            out[key] = val
    return out


def test_the_probe_asks_whether_a_silent_peer_is_alive(app, settings):
    device = app.state.lib.devices.config.by_id["kobo"]
    opts = _opts(ssh_argv(device, settings))
    assert opts["ServerAliveInterval"] == str(SERVER_ALIVE_INTERVAL)
    assert opts["ServerAliveCountMax"] == str(SERVER_ALIVE_COUNT_MAX)
    assert opts["BatchMode"] == "yes"


def test_a_transfer_carries_the_same_keepalives_as_the_probe(app, settings):
    """Both ride the one master per device, so they cannot hold different opinions about
    when it is dead — whichever opened it wins, and a disagreement is only ever a
    surprise."""
    lib = app.state.lib
    device = lib.devices.config.by_id["kobo"]
    argv = build_argv(device, lib.devices.config, ["Reference"], settings)

    ssh_e = argv[argv.index("-e") + 1]
    assert f"ServerAliveInterval={SERVER_ALIVE_INTERVAL}" in ssh_e
    assert f"ServerAliveCountMax={SERVER_ALIVE_COUNT_MAX}" in ssh_e

    probe_opts = _opts(ssh_argv(device, settings))
    assert f"ServerAliveInterval={probe_opts['ServerAliveInterval']}" in ssh_e
    assert f"ServerAliveCountMax={probe_opts['ServerAliveCountMax']}" in ssh_e


def test_the_window_is_shorter_than_the_freespace_interval(settings):
    """The point of setting these at all: a woken device must cost at most one stale
    reading, not three. Debian's BatchMode default is 300 x 3 = 900s, a quarter of an
    hour of the storage and battery cells being wrong with a green dot beside them."""
    window = SERVER_ALIVE_INTERVAL * SERVER_ALIVE_COUNT_MAX
    assert window < settings.freespace_interval, (
        f"a wedged master survives {window}s, longer than the "
        f"{settings.freespace_interval}s between readings"
    )


def test_the_window_is_long_enough_to_survive_ordinary_jitter():
    """The failure mode at the other end. These were once tried at 5 x 1 and had to be
    turned off: five seconds of silence is normal — rsync checksumming a large file, a
    stalled FAT write — and one unanswered probe then drops a working link. The interval
    only starts after *no data at all*, so a running transfer never triggers it, but the
    threshold still has to sit outside normal jitter."""
    assert SERVER_ALIVE_INTERVAL >= 30
    assert SERVER_ALIVE_COUNT_MAX >= 2
    assert SERVER_ALIVE_INTERVAL * SERVER_ALIVE_COUNT_MAX >= 120
