"""devices.yaml parsing, validation, and the line numbers the error strip needs."""

from __future__ import annotations

import pytest

from libnodes.models import TYPE_SEEDS, parse_devices, parse_size


def test_valid_file_parses():
    config, issues = parse_devices(
        """
defaults:
  retries: 3
devices:
  - id: kao
    name: Kobo Aura One
    type: kobo
    host: 192.168.1.42
    target: /mnt/onboard/Books
    full_sync: true
"""
    )
    assert issues == []
    assert config.defaults.retries == 3
    device = config.by_id["kao"]
    assert device.full_sync is True
    # Unset transport fields fall back to the type's seed.
    assert device.effective_port == TYPE_SEEDS["kobo"]["port"] == 2222
    assert device.effective_user == "root"


def test_schema_error_reports_the_offending_line():
    """The design's example: `devices[1].port` must be an integer, on line 22."""
    text = """
defaults:
  retries: 2

devices:
  - id: kao
    name: Kobo Aura One
    type: kobo
    host: 192.168.1.42
    port: 2222
    target: /mnt/onboard/Books

  - id: n20u
    name: Note 20
    type: termux
    host: 192.168.1.15
    port: "8022 "
    target: /sdcard/Books
"""
    config, issues = parse_devices(text)
    assert config is None
    assert len(issues) == 1
    issue = issues[0]
    assert issue.path == "devices[1].port"
    assert text.splitlines()[issue.line - 1].strip().startswith("port:")
    assert "8022 " in issue.message


def test_syntax_error_is_reported_not_raised():
    config, issues = parse_devices("devices:\n  - id: x\n   bad indent: y\n")
    assert config is None
    assert issues and issues[0].line is not None


def test_unknown_key_is_rejected():
    """extra=forbid: a typo silently ignored would be worse than a visible error."""
    _, issues = parse_devices(
        "devices:\n  - id: a\n    name: A\n    host: h\n"
        "    target: /t\n    prot: 22\n"
    )
    assert issues
    assert "prot" in issues[0].path or "prot" in issues[0].message


def test_port_range_validated():
    _, issues = parse_devices(
        "devices:\n  - id: a\n    name: A\n    host: h\n    target: /t\n    port: 99999\n"
    )
    assert issues
    assert issues[0].path == "devices[0].port"


def test_empty_file_is_valid():
    config, issues = parse_devices("")
    assert issues == []
    assert config.devices == []


@pytest.mark.parametrize(
    "field", ["formats: [epub]", "rsync_flags: ['-z']", "wol_mac: 'aa:bb'",
              "keep_free: 1G", "probe_interval: 60"],
)
def test_a_field_that_does_nothing_is_refused_with_its_line(field):
    """These were once accepted and ignored, so an edit to one looked like it did
    something. Refused now, with the line, in the chip the Devices page already shows."""
    config, issues = parse_devices(
        "devices:\n  - id: a\n    name: A\n    host: h\n    target: /t\n"
        f"    {field}\n"
    )
    assert config is None
    assert issues and issues[0].line == 6
    assert "extra inputs are not permitted" in issues[0].message


def test_parse_size():
    assert parse_size("29G") == 29 * 1024**3
    assert parse_size("1.5T") == int(1.5 * 1024**4)
    assert parse_size("512") == 512
    assert parse_size(None) is None
    assert parse_size("nonsense") is None


def test_defaults_are_inherited_not_copied():
    config, _ = parse_devices(
        """
defaults:
  retries: 5
devices:
  - id: a
    name: A
    host: h
    target: /t
  - id: b
    name: B
    host: h
    target: /t
    retries: 1
"""
    )
    defaults = config.defaults
    assert config.by_id["a"].retries_with(defaults) == 5
    assert config.by_id["b"].retries_with(defaults) == 1
