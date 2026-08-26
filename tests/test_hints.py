"""Failure hints: rsync's diagnostics are accurate but rarely name the real cause."""

from __future__ import annotations

import pytest

from libnodes.jobs import _hints, is_attrs_only


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


# ------------------------------------------- exit 23: files, or only attrs --

# The real log of job #1, pushing two books to nexus10 (Android 5.1). Both landed
# byte-exact -- verified with stat on the device -- and rsync still exited 23, because
# /sdcard is Android's FUSE emulation and its daemon does not implement utimensat.
ATTRS_ONLY_LOG = """\
@5117977|Science/Physics/General-Relativity/Abdildin/Mexanika-teorii-gravitacii.djvu
      5,117,977  83%  125.10MB/s    0:00:00 (xfr#1, to-chk=1/6)
@1002176|Science/Physics/General-Relativity/Abdildin/Problema-dvizhenija-tel-v-OTO-2006.pdf
      6,120,153 100%  121.55MB/s    0:00:00 (xfr#2, to-chk=0/6)
rsync: failed to set times on "/sdcard/.../Mexanika-teorii-gravitacii.djvu.rYNFG5": Operation not permitted (1)
rsync: failed to set times on "/sdcard/.../Problema-dvizhenija-tel-v-OTO-2006.pdf.gYd8W4": Operation not permitted (1)
sent 13,438 bytes  received 20,093 bytes  22,354.00 bytes/sec
rsync error: some files/attrs were not transferred (see previous errors) (code 23) at main.c(1347) [sender=3.4.1]
"""


def test_an_attrs_only_exit_23_is_not_a_partial_transfer():
    """The bug this was written for: every byte landed and the dock drew TRANSFER FAILED
    in red. rsync spends one exit code on two opposite outcomes; the diagnostics are what
    separate them."""
    assert is_attrs_only(ATTRS_ONLY_LOG) is True


def test_a_real_partial_transfer_still_says_so():
    """One non-attribute diagnostic is enough. A vanished source, an unreadable book and
    a full device all exit 23 too, and each of those genuinely did not deliver."""
    vanished = ATTRS_ONLY_LOG.replace(
        "sent 13,438",
        'rsync: link_stat "/Books/Science/Gone.pdf" failed: No such file or directory (2)\n'
        "sent 13,438",
    )
    assert is_attrs_only(vanished) is False


def test_an_exit_23_that_explains_nothing_stays_a_failure():
    """Conservative on purpose: with no diagnostic at all there is nothing to read, and
    guessing "probably fine" about a transfer is the wrong direction to guess in."""
    assert is_attrs_only(
        "sent 13,438 bytes  received 20,093 bytes\n"
        "rsync error: some files/attrs were not transferred (code 23)\n"
    ) is False
    assert is_attrs_only("") is False


def test_the_receivers_role_tag_is_optional():
    """It is the far side that reports this, and the rsync on a device is whatever it
    ships: 3.2+ tags the role, older builds do not."""
    tagged = ATTRS_ONLY_LOG.replace("rsync: failed", "rsync: [generator] failed")
    assert is_attrs_only(tagged) is True


def test_failing_to_set_times_names_its_cause(tmp_path):
    path = tmp_path / "job.log"
    path.write_text(ATTRS_ONLY_LOG)
    hints = _hints(path, 23)
    assert any("cannot store timestamps" in h for h in hints)
    # The consequence matters more than the cause -- the data is fine, but every later
    # push re-sends it -- and the fix matters more than either. A hint that names the
    # setting turns the next device that hits this into a one-line edit.
    assert any("re-send" in h for h in hints)
    assert any("stores_times: false" in h for h in hints)
