"""The devices.yaml schema, plus the YAML->line mapping the validation strip needs."""

from __future__ import annotations

import re
from typing import Any, Literal, Sequence

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

# devices.yaml is hand-edited, and YAML already yields native ints and bools for
# unquoted scalars. So a *string* reaching an int field means the user quoted it, and
# pydantic's default lax coercion would silently accept `port: "8022 "` — the exact
# mistake the design's validation strip is built to catch. Strict types make it an error.
Int = StrictInt
Bool = StrictBool

NodeType = Literal["kobo", "termux", "linux"]

#: What shape of the library a node wants, and -- since `upstream` -- which *direction*
#: it moves in. Three genuinely different transfers, not three styles of one; see
#: `Device.sync_mode`, `jobs.build_argv` and `jobs.build_pull_argv`.
SyncMode = Literal["books", "mirror", "upstream"]

#: Picking a node type seeds transport fields. The drawer surfaces this as a `warn`
#: note explaining what changed; here it only fills gaps the user left empty.
TYPE_SEEDS: dict[str, dict[str, Any]] = {
    "kobo": {
        "port": 2222,
        "user": "root",
        # dropbear on a Kobo predates most modern KEX/cipher defaults.
        "ssh_options": "-o KexAlgorithms=+diffie-hellman-group1-sha1 -o HostKeyAlgorithms=+ssh-rsa",
    },
    "termux": {"port": 8022, "user": "u0_a1", "ssh_options": ""},
    "linux": {"port": 22, "user": "root", "ssh_options": ""},
}

class FsProfile(BaseModel):
    """What a target filesystem can and cannot do, in the terms rsync cares about."""

    model_config = ConfigDict(frozen=True)

    #: Can it store unix ownership and permissions? On FAT rsync's chmod fails on every
    #: run and it then counts every file as changed: measured on a real Android SD card,
    #: one directory reported 43 items needing work with perms on and 0 with them off.
    #: One field, not two, because no filesystem here stores an owner without a mode —
    #: FAT takes both from the mount's uid=/gid=, so `build_argv` answers a False here
    #: with --no-perms --no-owner --no-group together. The owner half is the one that
    #: bit: the Kobo's vfat driver refuses chown even to root, so a push that delivered
    #: every byte still exited 23 and was retried three times.
    perms: bool = True
    #: Largest single file, if the filesystem imposes one. FAT32 stops at 4 GiB - 1.
    max_file: int | None = None
    #: Seconds of mtime slack to allow, for filesystems that cannot store the timestamp
    #: they were handed. 0 means compare exactly. See the FAT entries below.
    modify_window: int = 0
    note: str = ""


FS_PROFILES: dict[str, FsProfile] = {
    # Real unix filesystems: full archive semantics, nothing to work around.
    "ext4": FsProfile(),
    "ext3": FsProfile(),
    "ext2": FsProfile(),
    "xfs": FsProfile(),
    "btrfs": FsProfile(),
    "zfs": FsProfile(),
    "f2fs": FsProfile(),
    # FAT and friends. modify_window=1 is not caution, it is a measurement: on the FAT32
    # SD card in a real Android phone (466 GB, 32 KB clusters), mtimes come back rounded
    # down to an even second — 75-ores.mp3 was written at 09:37:25 and reads back
    # 09:37:24 — and rsync 3.1.3 compares them exactly. 8,786 of 24,620 files therefore
    # wanted re-sending on every single push, for ever; with the window, 0 did. This is
    # textbook FAT: the on-disk format stores the seconds field in units of two. An
    # earlier note here claimed the granularity "did not show up on the tested Android
    # device" on the strength of one odd-second timestamp appearing to round-trip. One
    # timestamp is not a sample.
    #
    # 1 second, not 2: rounding to an even second moves a timestamp by at most 1, and
    # rsync's window is symmetric. Keep it as tight as the hardware allows — this is the
    # check that notices a book edited in place.
    "vfat": FsProfile(perms=False, max_file=4 * 1024**3 - 1, modify_window=1, note="FAT32"),
    "fat32": FsProfile(perms=False, max_file=4 * 1024**3 - 1, modify_window=1, note="FAT32"),
    "msdos": FsProfile(perms=False, max_file=4 * 1024**3 - 1, modify_window=1, note="FAT"),
    # exFAT's format has a 10 ms field, so in principle it needs no slack — but drivers
    # that ignore it and fall back to FAT's two seconds are common, and the cost of the
    # window is far smaller than the cost of re-sending a library. Inferred, not
    # measured: the phone above is vfat.
    "exfat": FsProfile(perms=False, modify_window=1, note="exFAT"),
    # NTFS stores 100 ns and HFS+ whole seconds; neither needs slack, and both keep the
    # exact comparison.
    "ntfs": FsProfile(perms=False, note="NTFS"),
    "hfsplus": FsProfile(perms=False, note="HFS+"),
    # Anything unrecognised: assume it behaves, and let a failing sync say otherwise.
    "other": FsProfile(),
}


_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([KMGTP]?)i?B?\s*$", re.IGNORECASE)
_SIZE_MULT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}


def parse_size(value: str | int | None) -> int | None:
    """``"29G"`` -> bytes. Returns None for anything unparseable."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    m = _SIZE_RE.match(str(value))
    if not m:
        return None
    return int(float(m.group(1)) * _SIZE_MULT[m.group(2).upper()])


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Accepted but ignored. The transfer flags are the program's, not the config's:
    #: LibNodes depends on their exact effect (-L for the CAS symlinks, -R for the path
    #: shape, --info/--out-format for the progress parser), so a hand-edited value could
    #: break the app in ways that look like bugs. See jobs.BASE_FLAGS.
    rsync_flags: list[str] | None = None
    timeout: Int = 20
    retries: Int = 2
    bandwidth: str | None = None
    #: Applied to every transfer on top of the per-node list.
    excludes: list[str] = Field(default_factory=list)


class Device(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    abbr: str | None = None
    type: NodeType = "linux"
    host: str
    port: Int | None = None
    user: str | None = None
    identity: str | None = None
    target: str
    #: What to show instead of `target` in the UI. Purely cosmetic — never used to build
    #: an rsync or ssh command. Lets a Termux node read as `~/sd/Books` rather than
    #: `/data/data/com.termux/files/home/sd/Books`, which overflows every column it
    #: appears in.
    target_ui: str | None = None
    full_sync: Bool = False
    #: May a Full Sync *remove* what this node holds and the library no longer does?
    #:
    #: Off by default, and that default is the promise Full Sync has always made: adds
    #: and updates only. The flag is what buys `--delete`, and it is per node because the
    #: cost of getting it wrong is per node — a reader that is also somebody's scratch
    #: directory, or a KOReader device whose `.sdr` sidecars live *inside* the library
    #: tree, is not a thing to prune on a hunch. Say so here, and say in `excludes` what
    #: must survive: rsync never deletes what an `--exclude` matched.
    #:
    #: Only a `books` node with `full_sync: true` can use it, and only on the whole-library
    #: push. A subtree push is deliberately left alone — `--delete` prunes the directories
    #: in the transfer, so a Push of `Science/` would silently mean "and remove everything
    #: under Science/ that is not in the library", which is not what that button says.
    #: The scope is the same reason a mirror hands rsync `./`: what is transferred is what
    #: is pruned, so a top-level name the library does not have at all (s4l's `Websites/`)
    #: survives a Full Sync untouched.
    #:
    #: Coerced off for `mirror` and `upstream` below: a mirror's --delete is its mode, not
    #: this key, and an upstream is never written to at all.
    prune: Bool = False
    capacity: str | None = None
    keep_free: str | None = None
    wol_mac: str | None = None
    #: The target filesystem. Declare the fact; LibNodes decides the flags.
    #:
    #: This is what actually constrains a transfer, and device type is only a proxy for
    #: it — a Linux host can perfectly well have an exFAT disk mounted, and its sync
    #: should be treated accordingly. Recording the filesystem rather than a set of
    #: flags means a later discovery about, say, FAT's 4 GB file limit has somewhere
    #: obvious to live.
    #:
    #: Unset is inferred from `type`: kobo and termux targets are FAT in practice,
    #: linux is not. See FS_PROFILES.
    fs: str | None = None
    #: Which shape of the library this node wants. Declare what the node is *for*;
    #: LibNodes decides the flags and the source list.
    #:
    #:   books   A reader -- e-reader, tablet, phone. It wants the books themselves, so
    #:           the CAS symlinks are dereferenced (-L) and the infrastructure top-level
    #:           directories are not sent at all. This is what every device was until a
    #:           Linux node needed the other thing.
    #:
    #:   mirror  A replica. It wants /Books exactly as the Pi stores it: symlinks kept as
    #:           symlinks, the .data vault carried alongside them so they resolve, and
    #:           urantia-library/ included. No -L, no skiplist, and --delete, because a
    #:           replica that keeps files the origin dropped is not a replica.
    #:           Read `upstream` below before choosing this for a node other people
    #:           upload to: it was declared here for sigmaai.au, and what that put in
    #:           the Actions menu was `rsync -a --delete ./ tigran@sigmaai.au:/Books/`.
    #:
    #:   upstream  A source. The production library other people upload to, which means
    #:           it is *ahead* of us and a push would be a regression. LibNodes pulls
    #:           from it -- whole root, remote to local, no --delete and no -L -- and
    #:           refuses every writing action: at each route, and again in build_argv
    #:           itself, so a code path nobody remembered cannot compose one. Its tree
    #:           is our own CAS shape, so it scans like a mirror; see `cas_tree`.
    #:
    #: Not derived from `type`, for the same reason `fs` is not: type is a proxy, and a
    #: Linux host is perfectly entitled to want any of the three. A mirror node is also
    #: the only thing that may *receive* urantia-library/ -- an upstream holds one too
    #: and is deliberately excluded from the pull, see config.SKIP_TOPLEVEL and
    #: config.PULL_EXCLUDES, where both boundaries are documented.
    sync_mode: SyncMode = "books"
    #: Whether the target can store a modification time at all. Declare the fact; the
    #: flags follow.
    #:
    #: A fact about the *target path*, not about the device, the OS or `fs:` — which is
    #: the whole point of it being a separate key. Android splits into two cases and only
    #: one of them fails:
    #:
    #:   emulated  `/sdcard`, `/storage/emulated/0`. Not a filesystem at all but a FUSE
    #:             shim (`/dev/fuse`) with nothing underneath it, and its daemon does not
    #:             implement utimensat: EPERM to everyone, root included. nexus10 has only
    #:             this — its `/storage` holds `emulated` and a `sdcard0` alias of it, and
    #:             nothing else.
    #:
    #:   a card    vold mounts the real volume (`/dev/block/vold/public:179,65` on lg, a
    #:             466 GB vfat) with `allow_utime`, and times work straight through the
    #:             FUSE view of it. lg's `~/sd` is a symlink to `/storage/D94C-6302/...`,
    #:             so lg is a `vfat` Android node whose timestamps are fine.
    #:
    #: Verified 2026-08-26 with `touch -t` as root on a file root had just created: EPERM
    #: on nexus10's target and on lg's `/sdcard`, OK on lg's actual target. So two Android
    #: nodes that both declare `fs: vfat` differ, and the one that fails is not even
    #: writing to a filesystem. Deriving this from `fs:` would drop lg and the Kobo to a
    #: size-only comparison for no reason; test the target, not the platform.
    #:
    #: Leaving it true on such a node is not cosmetic. rsync's quick check is size+mtime,
    #: so a destination whose mtime is always the moment of transfer can never match, and
    #: every push re-sends the entire selection for ever. Measured with `-n -i` against
    #: files byte-identical to the source: `<f..t......`, the `<` being data on its way.
    #: See `build_argv`, which is also where the reason `--no-times` alone does not fix it
    #: is written down.
    stores_times: Bool = True
    #: A file on the device whose contents are the battery percentage, or unset for a
    #: node that has no battery worth reporting.
    #:
    #: A path rather than a flag, because there is no portable way to ask. Android exposes
    #: it under /sys/class/power_supply/, but the node name varies by vendor and kernel --
    #: `battery` on the LG G4s, `bms` or `battery_0` elsewhere -- and a Kobo running
    #: KOReader has a different tree again. Whoever edits devices.yaml can `cat` the file
    #: to check; LibNodes cannot guess it, and guessing wrong would report a confident
    #: wrong number rather than nothing.
    #:
    #: Read by the same ssh that runs `df`, so declaring it costs no extra round trip.
    #:
    #: The `status` file beside it is read too, for the charging bolt -- see
    #: `probe.charging_command` for why that sibling is derived rather than declared, and
    #: why the `online` files that look like the better question are not consulted.
    battery: str | None = None
    #: Where to read the charging state, when the sibling of `battery` is not it.
    #:
    #: The derivation is right for a device whose charge and charger are the same supply,
    #: which is most of them. The Nexus 10 is not one: its charge comes from a fuel gauge
    #: (`ds2784-fuelgauge`) that exposes no `status` at all, while the charger is a
    #: separate supply (`smb347-battery`) two directories away. No rule relates the two --
    #: the tablet also carries `manta-battery`, `smb347-mains` and `smb347-usb` -- so this
    #: is the case that has to be declared rather than worked out.
    #:
    #: Read exactly like `battery`: a path, quoted as one, `cat`ed on the same ssh.
    charging: str | None = None
    #: A command to run on the device instead, for a node whose charge is not a file.
    #:
    #: Android 12 does not let Termux read /sys/class/power_supply, so there is nothing to
    #: cat and the answer has to come from `termux-api BatteryStatus`, which prints JSON.
    #: A second field rather than a cleverer `battery`, because the two are not reliably
    #: distinguishable by inspection -- that termux-api invocation is itself an absolute
    #: path, with an argument -- and a wrong guess would report a wrong number rather than
    #: failing. Setting both is an error, not a precedence rule.
    #:
    #: Run through the device's shell, so it may be a pipeline. Note that a non-interactive
    #: ssh gets Termux's PATH but not its libexec, so `termux-api` needs its full path.
    battery_cmd: str | None = None
    #: Accepted but ignored. The design proposed a per-device format whitelist; in
    #: practice LibNodes pushes whatever you point it at, and deciding what a device can
    #: open is the device's business. Kept in the schema only so an older devices.yaml
    #: still loads instead of failing validation over a dead field.
    formats: list[str] | None = None
    ssh_options: str | None = None
    probe_interval: Int | float | None = None

    # Per-node overrides of `defaults`. Absent means "inherit".
    rsync_flags: list[str] | None = None   # accepted but ignored; see Defaults
    timeout: Int | None = None
    retries: Int | None = None
    bandwidth: str | None = None
    excludes: list[str] | None = None
    #: What a pull from this node holds back. Absent means "inherit config.PULL_EXCLUDES",
    #: which is where each entry's reason is written down. Separate from `excludes` and
    #: deliberately not merged with it: those ride on every push to a *device*, and this is
    #: a fact about what must not be copied *here*, which is a different question with a
    #: different failure mode.
    pull_excludes: list[str] | None = None

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", v):
            return v.strip().lower().replace(" ", "-")
        return v

    @field_validator("port")
    @classmethod
    def _port_range(cls, v: int | None) -> int | None:
        if v is not None and not (0 < v < 65536):
            raise ValueError("must be between 1 and 65535")
        return v

    @model_validator(mode="after")
    def _one_battery_source(self) -> "Device":
        """Reject both battery sources rather than quietly preferring one.

        A device declaring a file *and* a command is a half-finished edit -- someone
        moving a node from sysfs to termux-api and not deleting the old line. Whichever
        way a precedence rule fell it would be silently wrong half the time, and the row
        would show a plausible number from the source the editor thought they had
        replaced.
        """
        if self.battery and self.battery_cmd:
            raise ValueError(
                "set battery (a file to read) or battery_cmd (a command to run), "
                "not both"
            )
        # A charger source with nothing to hang it off is read by nobody: the readings
        # script only grows a `# power` section for a device that is already being asked
        # for its charge. Silently ignoring it would leave someone staring at a line they
        # had written and a column that never filled.
        if self.charging and not (self.battery or self.battery_cmd):
            raise ValueError(
                "charging needs a battery or battery_cmd to be read alongside"
            )
        return self

    @model_validator(mode="after")
    def _an_upstream_is_never_full_synced(self) -> "Device":
        """Force full_sync off for an upstream node rather than rejecting the file.

        full_sync is mutually exclusive with `mirror` by an explicit term in
        routes.devices.device_full_sync and an `elif` in device_menu.html -- Full Sync
        promises it never deletes, and a mirror's transfer is defined by --delete. That
        exclusivity is with *mirror*, not with "not a reader", so the moment a node
        stops being a mirror the `elif` fires and Full Sync -- a push -- reappears in
        its menu. sigmaai.au carried `full_sync: true` while it was a mirror, where the
        key did nothing; leaving it on the upstream entry would have swapped one push
        hazard for another.

        Coerced rather than raised: devices.yaml is hand-edited and hot-reloaded, so a
        raise here takes the whole fleet's config down over a stale key. The route guard
        and build_argv are the real defences; this is the one that makes the menu agree
        with them.
        """
        if self.sync_mode == "upstream":
            object.__setattr__(self, "full_sync", False)
        return self

    @model_validator(mode="after")
    def _only_a_reader_prunes(self) -> "Device":
        """`prune` is a fact about Full Sync, so it means nothing off a `books` node.

        A mirror already deletes — that is what `sync_mode: mirror` *is*, and reading a
        second key as though it turned that on or off would be a way to talk someone into
        believing `prune: false` made a Replicate safe. An upstream is never written to.
        Coerced rather than raised for the reason above: a hand-edited, hot-reloaded file
        must not take the fleet down over a stale key.
        """
        if self.sync_mode != "books":
            object.__setattr__(self, "prune", False)
        return self

    @field_validator("fs", mode="before")
    @classmethod
    def _normalise_fs(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower().lstrip(".")
        return v

    @field_validator("formats", mode="before")
    @classmethod
    def _lower_formats(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [str(x).lower().lstrip(".") for x in v]
        return v

    # --- derived ------------------------------------------------------------

    @property
    def display_abbr(self) -> str:
        return (self.abbr or self.id[:4]).upper()

    @property
    def display_target(self) -> str:
        """The path to show a human. Falls back to the real one when unset."""
        return self.target_ui or self.target

    @property
    def effective_port(self) -> int:
        return self.port if self.port is not None else TYPE_SEEDS[self.type]["port"]

    @property
    def effective_user(self) -> str:
        return self.user or TYPE_SEEDS[self.type]["user"]

    @property
    def effective_ssh_options(self) -> str:
        opts = self.ssh_options
        if opts is None:
            opts = TYPE_SEEDS[self.type]["ssh_options"]
        return opts

    @property
    def effective_fs(self) -> str:
        if self.fs:
            return self.fs
        # kobo onboard storage and Android SD cards are FAT (or a FUSE layer over it).
        return "ext4" if self.type == "linux" else "vfat"

    @property
    def fs_profile(self) -> "FsProfile":
        return FS_PROFILES.get(self.effective_fs, FS_PROFILES["other"])

    @property
    def is_mirror(self) -> bool:
        """One name for the mode, so argv, routes and templates cannot disagree.

        Strictly "replicated to, with --delete", and it must not be widened to mean
        "CAS-shaped" or "not a reader" -- `cas_tree` and `is_selectable` exist so it
        does not have to be. Overloading this is how /replicate comes back to life
        pointed at an upstream node.
        """
        return self.sync_mode == "mirror"

    @property
    def is_upstream(self) -> bool:
        """A pull source, and never a transfer destination."""
        return self.sync_mode == "upstream"

    @property
    def dom_id(self) -> str:
        """The id, made safe to put in a DOM id *and* in a CSS selector.

        An HTML id may legally contain a dot; a CSS id selector may not, unescaped. htmx
        spans both: `hx-target="#node-<id>"` is a querySelector, and -- the part that
        cannot be escaped around -- an out-of-band swap builds its own selector as
        `"#" + element.getAttribute("id")` and runs *that* through querySelectorAll. So the
        id attribute itself has to be selector-safe; escaping only the targets would leave
        every OOB refresh silently dropped.

        `sigmaai.au` is the id that found this. `#scan-status-sigmaai.au` parses as the id
        `scan-status-sigmaai` plus the class `au`, matches nothing, and htmx answers an
        unresolvable target by firing htmx:targetError and **not sending the request** --
        so Scan device on that node did nothing at all, with no request in the log to say
        why. Row Retry, card Retry and the Test dialog's out-of-band row refresh were
        broken the same way.

        Not solved by changing the id: it is the key in manifests.db, jobs.db and
        probe.json, and it is the hostname, which is the honest name for the node. This is
        a presentation concern, so it stays in the presentation layer. Every id already in
        use is unaffected -- `_slug` allows only [a-z0-9_-] before it falls back, so a dot
        is the one character that has ever reached here.
        """
        return re.sub(r"[^A-Za-z0-9_-]", "-", self.id)

    @property
    def cas_tree(self) -> bool:
        """This node's tree *is* the CAS shape: symlinks into .data/, vault and all.

        True of a mirror (we put that shape there) and of an upstream (it is where that
        shape comes from). One fact, three call sites that all need it -- the scan must
        ask rsync for link targets (scan.py, -l), must keep the link rows it gets back
        (`keep_links`), and the extras dialog must not call a correct vault 24.6k
        orphans. Miss any one of the three on an upstream and the failure is silent in
        the worst direction: every book there is a symlink, so a scan that drops links
        reports a full production library as an empty backlog.
        """
        return self.sync_mode in ("mirror", "upstream")

    @property
    def is_selectable(self) -> bool:
        """May this node be picked in the Library view and the job picker?

        Stated positively on purpose. The filters used to say `not d.is_mirror`, which
        silently admits any mode invented later -- `upstream` would have walked straight
        into the picker. Positive means a fourth mode is excluded until someone opts it
        in.
        """
        return self.sync_mode == "books"

    @property
    def capacity_bytes(self) -> int | None:
        return parse_size(self.capacity)

    @property
    def keep_free_bytes(self) -> int | None:
        return parse_size(self.keep_free)

    def excludes_with(self, defaults: Defaults) -> list[str]:
        own = self.excludes if self.excludes is not None else []
        return [*defaults.excludes, *own]

    def pull_excludes_with(self, fallback: "Sequence[str]") -> list[str]:
        """What a pull from this node holds back — its own list, or the program's.

        The fallback is passed in rather than imported: config.py imports this module, so
        reaching the other way for config.PULL_EXCLUDES would be a cycle. `jobs` imports
        both and is the only caller.

        Replaces rather than extends, unlike `excludes_with`. These are correctness
        boundaries with reasons attached (see config.PULL_EXCLUDES), so someone narrowing
        one of them needs to be able to see the whole list they are choosing, not discover
        that the entry they removed is still being appended from somewhere else.
        """
        if self.pull_excludes is not None:
            return list(self.pull_excludes)
        return list(fallback)

    def timeout_with(self, defaults: Defaults) -> int:
        return self.timeout if self.timeout is not None else defaults.timeout

    def retries_with(self, defaults: Defaults) -> int:
        return self.retries if self.retries is not None else defaults.retries

    def bandwidth_with(self, defaults: Defaults) -> str | None:
        return self.bandwidth if self.bandwidth is not None else defaults.bandwidth



class DevicesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaults: Defaults = Field(default_factory=Defaults)
    devices: list[Device] = Field(default_factory=list)

    @property
    def by_id(self) -> dict[str, Device]:
        return {d.id: d for d in self.devices}

    @model_validator(mode="after")
    def _dom_ids_stay_distinct(self) -> "DevicesFile":
        """Two ids must not collapse to one `dom_id`, or they share a row in the DOM.

        `dom_id` folds everything outside [A-Za-z0-9_-] to a dash, so `sigmaai.au` and a
        node called `sigmaai-au` would both render `id="node-sigmaai-au"` -- and every
        swap aimed at either would hit whichever came first. Vanishingly unlikely and
        cheap to rule out, and the validation strip is where a devices.yaml problem is
        supposed to appear.
        """
        seen: dict[str, str] = {}
        for device in self.devices:
            clash = seen.get(device.dom_id)
            if clash is not None:
                raise ValueError(
                    f"{device.id!r} and {clash!r} both render as {device.dom_id!r} in the "
                    "page — give one of them a different id"
                )
            seen[device.dom_id] = device.id
        return self

    @property
    def profiles(self) -> int:
        return len({d.type for d in self.devices})


class ValidationIssue(BaseModel):
    """One row for the devices.yaml validation strip."""

    path: str
    line: int | None
    message: str

    @property
    def label(self) -> str:
        return f"line {self.line}:" if self.line else "error:"


def _format_loc(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


def _locate_line(root: yaml.Node | None, loc: tuple[Any, ...]) -> int | None:
    """Walk a composed YAML node tree along `loc` and report the 1-based line."""
    if root is None:
        return None
    cur = root
    for key in loc:
        if isinstance(cur, yaml.MappingNode):
            for k, v in cur.value:
                if k.value == str(key):
                    cur = v
                    break
            else:
                break
        elif isinstance(cur, yaml.SequenceNode):
            if isinstance(key, int) and 0 <= key < len(cur.value):
                cur = cur.value[key]
            else:
                break
        else:
            break
    return cur.start_mark.line + 1


def parse_devices(text: str) -> tuple[DevicesFile | None, list[ValidationIssue]]:
    """Parse and validate devices.yaml.

    Returns ``(config, issues)``. A syntax error yields ``(None, [issue])``; schema
    errors yield ``(None, issues)`` so the caller can keep serving the last good
    config while the strip explains what broke.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        line = None
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            line = mark.line + 1
        problem = getattr(exc, "problem", None) or str(exc)
        return None, [ValidationIssue(path="", line=line, message=problem.strip())]

    if data is None:
        return DevicesFile(), []
    if not isinstance(data, dict):
        return None, [
            ValidationIssue(path="", line=1, message="top level must be a mapping")
        ]

    try:
        node_root = yaml.compose(text)
    except yaml.YAMLError:
        node_root = None

    try:
        return DevicesFile.model_validate(data), []
    except ValidationError as exc:
        issues: list[ValidationIssue] = []
        for err in exc.errors():
            loc = err["loc"]
            got = err.get("input")
            message = f"{_format_loc(loc)} {err['msg'].lower()}"
            if got is not None and not isinstance(got, (dict, list)):
                message += f" — got {got!r}"
            issues.append(
                ValidationIssue(
                    path=_format_loc(loc),
                    line=_locate_line(node_root, loc),
                    message=message,
                )
            )
        return None, issues


__all__ = [
    "Defaults",
    "Device",
    "DevicesFile",
    "NodeType",
    "SyncMode",
    "TYPE_SEEDS",
    "ValidationIssue",
    "parse_devices",
    "parse_size",
]
