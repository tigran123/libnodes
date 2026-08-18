"""Settings, and the load/validate/watch cycle for devices.yaml.

Every path the app touches is settable through a ``LIBNODES_``-prefixed environment
variable (or a ``.env`` file), so the same tree runs against the real ``/Books`` on the
Pi and against a fixture tree in tests.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import DevicesFile, ValidationIssue, parse_devices

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Top-level names that are never part of the browsable library. Mirrors
# _TOPDIR_SKIPLIST in /Books/urantia-library/webapp/backend/config.py:34, plus the
# entries in /Books/urantia-library/exclude.txt.
#
# This list governs *browsing*, and browsing is what makes it a boundary rather than a
# preference: the index walk applies it at depth 0 (library.py), and the push path admits
# only paths the index vouches for (routes/jobs.py `_resolve`). Nothing here can be
# reached by browsing, searching or selecting, and that has not changed.
#
# What did change: a `sync_mode: mirror` node -- and only such a node -- is sent these
# paths deliberately, by `jobs.mirror_sources`, which does not consult this list at all.
# So the rule is "never browsable, and pushable only to a device that names the mode",
# not "never pushable". A reader still cannot receive any of it.
#
# Three of these entries are load-bearing, not housekeeping:
#
#   urantia-library  A sibling application that lives inside the library root: the
#                    webapp that owns the catalog. Its tree holds source, configuration
#                    and potentially credentials, none of which is a book. Removing it
#                    from this set would make all of that browsable, and pushable to
#                    every device. Do not. A mirror node receives it because a verbatim
#                    replica of the Pi's /Books is the whole point of that mode, which is
#                    also its whole cost: declaring `sync_mode: mirror` is declaring that
#                    this node may hold the credentials. Nothing else grants that.
#
#   .data            Skipped for *browsing* only. It holds the actual bytes every
#                    library symlink points at, and rsync -L dereferences into it when
#                    transferring -- so it must stay out of the tree without being
#                    excluded from transfers. For a mirror it inverts from permitted to
#                    mandatory: that transfer keeps the symlinks, so without the vault
#                    beside them every one of them dangles.
#
#   Recommended      Not a place, a *category*. urantia-library calls it a "pseudo-
#                    directory managed exclusively by the recommend/unrecommend
#                    endpoints" (RECOMMENDED_SUBDIR in its config.py) and fills it with
#                    companion symlinks to books that already live elsewhere in the
#                    tree. Verified: all three of its entries point at blobs also
#                    reachable under Religions/ and Science/. Because we transfer with
#                    -L, syncing it would ship a complete SECOND copy of every
#                    recommended book to the device. That reason is specific to -L: a
#                    mirror preserves the companion links as links, so they cost a few
#                    hundred bytes and belong in a replica.
SKIP_TOPLEVEL = frozenset(
    {
        ".data",
        "urantia-library",
        "Recommended",
        "CLAUDE.md",
        "GEMINI.md",
        ".claude",
        ".vscode",
        ".antigravitycli",
        "exclude.txt",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LIBNODES_", env_file=".env", extra="ignore"
    )

    # --- filesystem -----------------------------------------------------------
    library_root: Path = Path("/Books")
    state_dir: Path = PROJECT_ROOT / "var"
    devices_file: Path | None = None
    #: urantia-library's catalog, read-only and entirely optional. When present it
    #: supplies title/author for indexed entries; when absent we fall back to filenames.
    catalog_db: Path = Path("/Books/.data/db/lib.db")

    # --- background work ------------------------------------------------------
    concurrency: int = 1
    probe_interval: float = 10.0
    probe_timeout: float = 2.0
    #: A node that answered within this many seconds but not now reads as "sleeping"
    #: rather than "offline" -- the Termux/Kobo suspend case.
    sleeping_window: float = 1800.0
    freespace_interval: float = 300.0
    #: A node that keeps failing is probed exponentially less often, up to this ceiling.
    #: Without it, twenty dead devices cost twenty connect attempts every probe_interval
    #: for as long as the service runs.
    probe_backoff_max: float = 300.0
    #: The same ceiling, for a fleet someone is actually looking at. The backoff is a cost
    #: control, but its cost is only ever *paid* by a person watching a red dot that will
    #: not go green: at 300s a device that came back stayed red for up to five minutes
    #: while the browser dutifully re-rendered the stale reading every 10s. That is what
    #: this exists to stop, and it is charged only while a Devices page is polling.
    probe_backoff_watched: float = 30.0
    #: How long a Devices request keeps the fleet "watched" after the last one. Sized
    #: against what a browser actually does, not against the template: `every 10s` holds
    #: only while the tab is in front. Backgrounded, the browser throttles the timer to
    #: once a minute -- measured on the Pi's journal, 10s intervals from 09:08:01 to
    #: 09:10:11 and exactly 60s from 09:11:01 on, same tab. At 60 this would sit on that
    #: boundary and flap between the two ceilings; 150 leaves two and a half throttled
    #: polls of margin and still relaxes a couple of minutes after the last tab closes.
    #: A backgrounded tab counting as watched is the point -- you alt-tab back to a page
    #: that is current, which is the whole complaint.
    watch_window: float = 150.0
    reindex_interval: float = 1800.0
    reindex_on_start: bool = True

    # --- limits ---------------------------------------------------------------
    term_ring: int = 500
    log_retention: int = 200

    # --- serving --------------------------------------------------------------
    host: str = "0.0.0.0"
    # LAN only on pi5, with nothing in front: nginx owns 80/443 for urantia-library, which
    # itself sits on 8000. 8090 was already the port on the old Pi, where 8080 was nginx's.
    port: int = 8090

    # --- access ---------------------------------------------------------------
    #: The single shared password. Empty means no login at all, which is what keeps a
    #: dev server and the test suite working unchanged -- and it is fail-open, so
    #: create_app() warns loudly at startup when it is unset.
    #:
    #: SecretStr rather than str because base_context puts this whole object into every
    #: template context (deps.py:38). A stray {{ settings }} in any template would
    #: otherwise print the password into the page; SecretStr renders `**********`.
    password: SecretStr = SecretStr("")
    #: How long "stay signed in" lasts. Long by design: the point is to be asked once per
    #: browser, not to expire people out of a household tool.
    session_days: float = 30.0

    @property
    def auth_enabled(self) -> bool:
        return bool(self.password.get_secret_value())

    @property
    def resolved_devices_file(self) -> Path:
        return self.devices_file or (self.state_dir / "devices.yaml")

    @property
    def index_db(self) -> Path:
        return self.state_dir / "index.db"

    @property
    def jobs_db(self) -> Path:
        return self.state_dir / "jobs.db"

    @property
    def manifests_db(self) -> Path:
        return self.state_dir / "manifests.db"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def probe_cache(self) -> Path:
        """Last session's device readings. A cache, not state: safe to delete, and the
        app starts with a blank fleet if it is missing. JSON rather than a fourth SQLite
        file because nothing queries it -- it is written once at shutdown and read once at
        startup. Lives beside devices.yaml, which the config watcher filters by name, so
        writing it does not trip a config reload."""
        return self.state_dir / "probe.json"

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


SEED_DEVICES_YAML = """\
# LibNodes devices. Written once, on first run -- edit it and the change takes effect
# immediately; LibNodes watches the file.
#
# type: kobo   -> KOReader/dropbear, conventionally port 2222 as root
#       termux -> Android/Termux sshd, conventionally port 8022
#       linux  -> ordinary sshd on port 22
#
# fs:   the TARGET filesystem: vfat, exfat, ntfs, ext4, btrfs, xfs...
#       LibNodes derives the rsync flags from it (FAT cannot store permissions, and
#       FAT32 cannot hold a file of 4 GiB or more). Optional: unset means vfat for
#       kobo/termux and ext4 for linux.
#
# sync_mode: which shape of the library this node wants. Optional; default `books`.
#
#       books   A reader. It gets the books themselves: the library is symlinks into a
#               content-addressed vault, so rsync -L copies the bytes through them, and
#               the infrastructure directories (.data, urantia-library, Recommended) are
#               not sent at all. Push whole categories or individual books.
#
#       mirror  A replica -- an ordinary Linux box that wants /Books exactly as it is
#               here. Symlinks stay symlinks, .data comes with them so they resolve,
#               urantia-library comes too, and --delete removes whatever the origin no
#               longer has. It is all-or-nothing: a mirror is not offered in the Library
#               view's push targets, it has one Replicate action on its device row.
#
#               Note what that means before setting it: this node will hold a copy of
#               urantia-library, configuration and credentials included, and Replicate
#               will delete files there that do not exist here. Run its Dry run first --
#               that is the only preview of the prune.
#
# battery: a file on the DEVICE holding the charge percentage, shown as a bar beside
#       storage. A path, because there is no portable way to ask: Android keeps it under
#       /sys/class/power_supply/ but the node name varies by vendor -- `battery` on an
#       LG G4, `BAT1` on a ThinkPad, `bms` or `battery_0` elsewhere. `cat` it over ssh to
#       check first; unset simply leaves the column empty.
#
#       The `status` file next to it is read as well, and puts a lightning bolt beside the
#       percentage: amber while charging, green while on the charger and full. Nothing to
#       declare -- sysfs keeps both files in the one supply directory -- and a device
#       without one simply gets no bolt.
#
# charging: where to read the charger, when it is NOT beside `battery`. Rare, and the
#       Nexus 10 is the reason it exists: its charge comes from a fuel gauge with no
#       `status` file, while the charger is a separate supply among five on that tablet.
#
#         battery:  /sys/class/power_supply/ds2784-fuelgauge/capacity
#         charging: /sys/class/power_supply/smb347-battery/status
#
#       Find it with `grep . /sys/class/power_supply/*/status` over ssh and check it
#       changes when you plug the charger in -- some of these nodes are stubs that read
#       `Charging` for ever.
#
# battery_cmd: a command to run instead, for a device where the charge is not a file.
#       Android 12 does not let Termux read /sys/class/power_supply at all, so there the
#       answer comes from termux-api, which prints JSON:
#
#         battery_cmd: /data/data/com.termux/files/usr/libexec/termux-api BatteryStatus
#
#       A bare number or a JSON object is understood; in JSON the first of percentage,
#       capacity, level or battery_level that holds a number in 0..100 is taken, and
#       `plugged`/`status` give the charging bolt at no extra cost. Give the full path --
#       a non-interactive ssh gets Termux's PATH but not its libexec.
#       Set battery or battery_cmd, never both.
#
#       Either way it is read by the same ssh that runs df, so it costs no extra round
#       trip.
#
# The entries below are examples. Replace them.

defaults:
  # No rsync flags here: LibNodes builds the transfer command itself, because it
  # depends on the exact behaviour of -L, -R and --out-format. What remains tunable is
  # everything that is genuinely a preference.
  timeout: 20
  retries: 2
  # bandwidth: 2M        # --bwlimit, per device or here for all
  # excludes: ["*.tmp"]  # extra --exclude patterns

devices:
  - id: reader
    name: E-reader
    abbr: EPUB
    type: kobo
    host: 192.168.0.10
    port: 2222
    user: root
    # A leading dot keeps the stock firmware from indexing the library on boot.
    target: /mnt/onboard/.Books
    target_ui: /onboard/.Books
    fs: vfat
    full_sync: true
    capacity: 29G

  - id: phone
    name: Phone
    abbr: PHON
    type: termux
    host: phone.lan
    port: 8022
    target: /data/data/com.termux/files/home/sd/Books
    target_ui: ~/sd/Books
    fs: vfat
    full_sync: false
    battery: /sys/class/power_supply/battery/capacity

  # A Linux box kept as a verbatim replica rather than stocked with books. Contrast the
  # two entries above: same program, two entirely different transfers.
  - id: mirror
    name: Linux mirror
    abbr: MIRR
    type: linux
    host: mirror.lan
    user: books
    target: /srv/books
    fs: ext4
    sync_mode: mirror
"""


class DevicesStore:
    """Holds the parsed devices.yaml and reloads it when its mtime moves.

    The file is the single source of truth for the Devices view and is hand-edited on the
    Pi, so we never cache across an edit. Parse failures are kept, not raised: the
    devices.yaml view renders the previous good config plus the error strip.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._config = DevicesFile()
        self._issues: list[ValidationIssue] = []
        self._text = ""

    def seed_if_missing(self) -> None:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(SEED_DEVICES_YAML, encoding="utf-8")

    def _stat_mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def reload(self, force: bool = False) -> None:
        with self._lock:
            mtime = self._stat_mtime()
            if not force and mtime == self._mtime:
                return
            self._mtime = mtime
            try:
                text = self.path.read_text(encoding="utf-8")
            except OSError as exc:
                self._text = ""
                self._issues = [ValidationIssue(path="", line=None, message=str(exc))]
                return
            self._text = text
            config, issues = parse_devices(text)
            self._issues = issues
            if config is not None:
                self._config = config

    @property
    def config(self) -> DevicesFile:
        self.reload()
        return self._config

    @property
    def issues(self) -> list[ValidationIssue]:
        self.reload()
        return self._issues

    @property
    def text(self) -> str:
        self.reload()
        return self._text

    @property
    def mtime(self) -> float | None:
        self.reload()
        return self._mtime

    def device(self, device_id: str):
        return self.config.by_id.get(device_id)


@lru_cache(maxsize=1)
def get_devices() -> DevicesStore:
    settings = get_settings()
    settings.ensure_dirs()
    store = DevicesStore(settings.resolved_devices_file)
    store.seed_if_missing()
    store.reload(force=True)
    return store


def reset_caches() -> None:
    """Drop memoised settings/stores. Used by tests that repoint env vars."""
    get_settings.cache_clear()
    get_devices.cache_clear()


__all__ = [
    "PROJECT_ROOT",
    "SKIP_TOPLEVEL",
    "Settings",
    "DevicesStore",
    "get_settings",
    "get_devices",
    "reset_caches",
]
