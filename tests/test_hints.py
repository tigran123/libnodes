"""Failure hints: rsync's diagnostics are accurate but rarely name the real cause."""

from __future__ import annotations

import pytest

from libnodes.jobs import _hints


@pytest.mark.parametrize(
    "log,expected_fragment",
    [
        (
            'rsync: mkdir "/sdcard/Books" failed: No such file or directory (2)\n'
            "rsync error: error in file IO (code 11) at main.c(682)",
            "parent directory does not exist",
        ),
        (
            "rsync: mkdir failed: Read-only file system (30)",
            "mounted read-only",
        ),
        (
            "root@192.168.1.42: Permission denied (publickey).",
            "no usable key",
        ),
        (
            "ssh: connect to host 192.168.1.31 port 8022: Connection refused",
            "Termux sshd stops when the device sleeps",
        ),
        (
            "ssh: connect to host 192.168.1.31 port 8022: No route to host",
            "DHCP lease may have moved",
        ),
        (
            "rsync: write failed: No space left on device (28)",
            "device is full",
        ),
        (
            "packet_write_wait: Connection to 192.168.1.31 port 8022: Broken pipe",
            "byte-accurate",
        ),
    ],
)
def test_known_failures_get_a_hint(tmp_path, log, expected_fragment):
    path = tmp_path / "job.log"
    path.write_text(log)
    hints = _hints(path, 11)
    assert any(expected_fragment in h for h in hints), hints


def test_unknown_255_falls_back_to_a_generic_hint(tmp_path):
    path = tmp_path / "job.log"
    path.write_text("something nobody has seen before")
    assert _hints(path, 255) == ["ssh itself failed — the node is probably unreachable"]


def test_clean_log_gets_no_hints(tmp_path):
    path = tmp_path / "job.log"
    path.write_text("sending incremental file list\nsent 100 bytes\n")
    assert _hints(path, 1) == []


def test_hints_are_capped(tmp_path):
    """Two hints is guidance; six is noise."""
    path = tmp_path / "job.log"
    path.write_text(
        "No such file or directory\nRead-only file system\n"
        "Permission denied\nConnection refused\nNo route to host\n"
    )
    assert len(_hints(path, 11)) == 2


def test_missing_log_is_not_fatal(tmp_path):
    assert _hints(tmp_path / "nope.log", 12) == []
