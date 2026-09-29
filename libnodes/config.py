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

# Top-level names that are never part of the browsable library (urantia-library's own
# _TOPDIR_SKIPLIST plus its exclude.txt). A security boundary, not housekeeping: the index
# walk drops them at depth 0, and a push admits only what the index vouches for
# (`routes/jobs._resolve`), so none of this can be browsed, searched or selected. Only a
# `sync_mode: mirror` node receives them, by `jobs.mirror_sources`, which ignores this list.
#
#   urantia-library  The sibling webapp: source, configuration and credentials. Declaring a
#                    node a mirror is declaring it may hold them.
#   .data            The vault. Hidden from browsing only; -L dereferences into it, and a
#                    mirror, which keeps the links, needs it sent.
#   Recommended      Companion symlinks to books that live elsewhere: with -L, a second full
#                    copy of every recommended book. As a mirror's links, a few hundred bytes.
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


# What a *pull* holds back: a different question from SKIP_TOPLEVEL's, so do not merge
# them. A pull wants `.data/` (every incoming link resolves into it) and `Recommended/`.
# Anchored, because the transfer root is the library root. rsync never deletes what an
# exclude matched, so these also survive the pull's --delete.
#
#   /urantia-library/  This host's own instance: its secrets.env holds per-host URLs, and
#                      pulling production's would point pi5's site at production.
#   /Unsorted/         55 GB of OS images on sigmaai.au (2026-09-14). The one preference
#                      rather than a boundary; narrow it in devices.yaml if books land there.
#   /.data/staging/    Half-written uploads: a torn blob would not hash to its own name.
#
# The catalog files come across in their own phase, under a quiet window; see
# `jobs._catalog_excludes`.
PULL_EXCLUDES = ("/urantia-library/", "/Unsorted/", "/.data/staging/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LIBNODES_", env_file=".env", extra="ignore"
    )

    # --- filesystem -----------------------------------------------------------
    library_root: Path = Path("/Books")
    state_dir: Path = PROJECT_ROOT / "var"
    devices_file: Path | None = None
    #: urantia-library's catalog, optional: titles and authors for the index.
    catalog_db: Path = Path("/Books/.data/db/lib.db")

    # --- background work ------------------------------------------------------
    concurrency: int = 1
    probe_interval: float = 10.0
    probe_timeout: float = 2.0
    #: A node that answered within this many seconds reads as amber "sleeping", not red.
    sleeping_window: float = 1800.0
    freespace_interval: float = 300.0
    #: The backoff ceiling for a node that keeps failing.
    probe_backoff_max: float = 300.0
    #: The ceiling while a Devices page is polling: at 300 s a device that came back stayed
    #: red for up to five minutes in front of someone watching it.
    probe_backoff_watched: float = 30.0
    #: How long one Devices request keeps the fleet "watched". A background tab's timer is
    #: throttled to once a minute (measured in the journal), so 150 s keeps it watched with
    #: margin and relaxes soon after the last tab closes.
    watch_window: float = 150.0
    reindex_interval: float = 1800.0
    reindex_on_start: bool = True

    # --- local services -------------------------------------------------------
    #: The unit **on this host** that reads `catalog_db`, stopped while a pull swaps it and
    #: started again in a `finally`. With its `.service` suffix, to match the polkit rule.
    #: Empty means none declared, and a Pull then skips the catalog phase and says so --
    #: overwriting a live WAL database under a reader is the corruption this prevents.
    #: Empty by default because the default is not the deployment; pi5's unit sets it.
    local_service: str = ""

    # --- limits ---------------------------------------------------------------
    term_ring: int = 500
    log_retention: int = 200
    #: The most one Pull may prune from *this host's* library. An upstream that is half
    #: mounted presents an almost empty list, whose honest reading is "delete everything";
    #: 1000 clears ordinary cleanup (three objects after four months, 2026-09-19) and stops
    #: that. Hitting it is rsync exit 25, not retried. Negative means uncapped; zero is
    #: rsync's own "delete nothing, but say if you would have".
    pull_max_delete: int = 1000

    # --- serving --------------------------------------------------------------
    host: str = "0.0.0.0"
    # LAN only; nginx and urantia-library own 80/443 and 8000 on pi5.
    port: int = 8090

    # --- access ---------------------------------------------------------------
    #: The shared password. Empty means no login at all -- fail-open, warned at startup.
    #: SecretStr, because `settings` is in every template context.
    password: SecretStr = SecretStr("")
    #: How long "stay signed in" lasts: once per browser, not once per visit.
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
        """Last session's device readings: a cache, safe to delete, written at shutdown and
        read at startup."""
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
  # excludes: ["*.tmp"]  # extra --exclude patterns, on top of each node's own
  #
  # An exclude is also a *protection*: rsync never deletes what one matched, so on a
  # `prune: true` node this is what survives a Full Sync. KOReader writes a `<book>.sdr`
  # directory beside each book it has opened, holding the reading position, bookmarks and
  # highlights — inside the library tree, so a prune removes them unless they are named
  # here. Measured against one device: 20 deletions without it, 1 with.
  # excludes: ["*.sdr/"]

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
    # Make the device *match* the library rather than only accumulate from it: a Full Sync
    # then carries --delete and removes what this node holds and the library no longer
    # does. Off by default, because Full Sync's standing promise is adds-and-updates-only
    # and a node that has not said this keeps it. Only the whole-library push prunes — a
    # Push of one directory never does — and only inside the categories it transfers, so a
    # top-level directory the library does not have is untouched. Pair it with `excludes`:
    # what they match is what survives.
    prune: true
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

  # The library's *source*: the production host other people upload to, so it is ahead of
  # us and a push would be a regression. LibNodes only ever pulls from this one, and
  # refuses every writing action -- Push, Full Sync, Replicate and Adopt -- at the route
  # and again in build_argv, so a code path nobody remembered cannot compose one.
  #
  # Declaring this is how you take a node out of harm's way. sigmaai.au was declared
  # `mirror`, which put Replicate in its Actions menu, and that command was
  # `rsync -a --delete ./ tigran@sigmaai.au:/Books/` -- it would have deleted every book
  # production held and this host did not. Do not use `mirror` for a node other people
  # upload to.
  #
  # `full_sync:` has no meaning here and is coerced off: it gates an action a non-`books`
  # node is never offered.
  - id: source
    name: Upstream library
    abbr: SRC
    type: linux
    host: books.example.org
    user: books
    target: /Books
    fs: ext4
    sync_mode: upstream
    # Optional. Defaults to config.PULL_EXCLUDES; override to narrow one of them.
    # pull_excludes: ["/urantia-library/", "/Unsorted/Ubuntu26-Portable/", "/.data/staging/"]
"""


class DevicesStore:
    """The parsed devices.yaml, reloaded when its mtime moves.

    A failed parse keeps the previous good config serving, and the Devices chip names every
    issue: raising would take the page down over a typo nothing in the app can fix.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._config = DevicesFile()
        self._issues: list[ValidationIssue] = []

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
                self._issues = [ValidationIssue(path="", line=None, message=str(exc))]
                return
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
