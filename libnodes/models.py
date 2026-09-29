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

# YAML already yields ints and bools for unquoted scalars, so a string here means it was
# quoted -- `port: "8022 "` -- which lax coercion would silently accept.
Int = StrictInt
Bool = StrictBool

NodeType = Literal["kobo", "termux", "linux"]

#: Three different transfers, not three styles of one: see `Device.sync_mode`.
SyncMode = Literal["books", "mirror", "upstream"]

#: Transport defaults per node type, filling only what a device leaves unset.
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

    #: Can it store unix ownership and permissions? One field, because no filesystem here
    #: stores an owner without a mode: False means --no-perms --no-owner --no-group
    #: together (see `build_argv` for the chown re-send loop the owner half caused).
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
    # FAT stores seconds in twos: on a real FAT32 card 75-ores.mp3, written at 09:37:25,
    # reads back 09:37:24, and 8,786 of 24,620 files were re-sent on every push; with the
    # window, 0. One second, not two: rounding moves a time by at most 1 and the window is
    # symmetric, and it should stay tight enough to notice a book edited in place.
    "vfat": FsProfile(perms=False, max_file=4 * 1024**3 - 1, modify_window=1, note="FAT32"),
    "fat32": FsProfile(perms=False, max_file=4 * 1024**3 - 1, modify_window=1, note="FAT32"),
    "msdos": FsProfile(perms=False, max_file=4 * 1024**3 - 1, modify_window=1, note="FAT"),
    # exFAT has a 10 ms field, but drivers that fall back to FAT's two seconds are common
    # and the window costs far less than a re-sent library. Inferred, not measured.
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
    #: What the UI shows instead of `target` (`~/sd/Books` for a long Termux path). Never
    #: used to build a command.
    target_ui: str | None = None
    full_sync: Bool = False
    #: May a Full Sync *remove* what this node holds and the library no longer does? Off by
    #: default, which keeps Full Sync's adds-and-updates-only promise. Pair it with
    #: `excludes` -- what they match survives (KOReader's `.sdr` sidecars live inside the
    #: tree). Only a Full Sync carries it; see `build_argv` for its scope. Coerced off for
    #: a mirror (whose --delete is its mode) and an upstream (never written to).
    prune: Bool = False
    capacity: str | None = None
    #: The target filesystem, the fact that actually constrains a transfer (a Linux host may
    #: well have an exFAT disk). Unset: FAT for kobo and termux, ext4 for linux.
    fs: str | None = None
    #: What the node is *for*; LibNodes derives the flags and the source list.
    #:
    #:   books     A reader. The books themselves (-L), without the infrastructure.
    #:   mirror    A replica of /Books as stored here: symlinks kept, the vault and
    #:             urantia-library/ included, and --delete.
    #:   upstream  The library's *source*, which other people upload to: pulled from
    #:             (whole root, with --delete, bounded) and never written to. sigmaai.au
    #:             was once declared `mirror`, which offered
    #:             `rsync -a --delete ./ tigran@sigmaai.au:/Books/` in its menu.
    #:
    #: Not derived from `type`, which is only a proxy.
    sync_mode: SyncMode = "books"
    #: Can the target *path* store an mtime? A fact about the path, not the device or `fs:`.
    #: Android's emulated storage (`/sdcard`) is a FUSE shim with no utimensat, EPERM even
    #: to root; a physical card vold mounts with `allow_utime` is fine. Verified with
    #: `touch -t` as root on 2026-08-26: nexus10's target fails, lg's card target works,
    #: and both are `fs: vfat`. Left true where it is false, every push re-sends the whole
    #: selection; see `build_argv` for the two flags it needs. Test the target:
    #: `ssh -n <node> 'F=<target>/.ut; touch "$F" && (touch -t 202001010101 "$F" && echo OK
    #: || echo EPERM); rm -f "$F"'`.
    stores_times: Bool = True
    #: A file on the device holding the charge percentage. A path, because the sysfs name
    #: varies by vendor and a guess would be a confident wrong number. Read on the same ssh
    #: as `df`; the `status` beside it gives the charging bolt (`probe.charging_command`).
    battery: str | None = None
    #: The charger's `status` file, when it is not beside `battery` -- the Nexus 10 reads
    #: its charge from a fuel gauge with no status while its charger is another supply.
    charging: str | None = None
    #: A command printing the charge instead, for a node where it is not a file (Termux on
    #: Android 12: `termux-api BatteryStatus`, by full path). Run by the device's shell, so
    #: it may be a pipeline. A separate key, because a path and a command cannot be told
    #: apart reliably; setting both is an error.
    battery_cmd: str | None = None
    ssh_options: str | None = None
    # `formats`, `rsync_flags`, `wol_mac`, `keep_free` and a per-device `probe_interval` were
    # once accepted and ignored; `extra="forbid"` now names any of them in the Devices chip.

    # Per-node overrides of `defaults`. Absent means "inherit".
    timeout: Int | None = None
    retries: Int | None = None
    bandwidth: str | None = None
    excludes: list[str] | None = None
    #: What a pull from this node holds back; absent means config.PULL_EXCLUDES, where each
    #: entry's reason is. Not merged with `excludes`, which ride on pushes to a device.
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
        """Reject both battery sources -- a half-finished edit, which any precedence rule
        would get silently wrong -- and a charger source with no charge to ride along."""
        if self.battery and self.battery_cmd:
            raise ValueError(
                "set battery (a file to read) or battery_cmd (a command to run), "
                "not both"
            )
        if self.charging and not (self.battery or self.battery_cmd):
            raise ValueError(
                "charging needs a battery or battery_cmd to be read alongside"
            )
        return self

    @model_validator(mode="after")
    def _an_upstream_is_never_full_synced(self) -> "Device":
        """Force full_sync off for an upstream, so the menu cannot offer it Full Sync.

        The menu's Full Sync branch excludes only *mirror*, so a node turned from mirror to
        upstream would otherwise grow a push to production. Coerced, not raised: a raise
        in a hot-reloaded file takes the whole fleet's config down over a stale key. The
        route and build_argv are the real guards; this keeps the menu agreeing.
        """
        if self.sync_mode == "upstream":
            object.__setattr__(self, "full_sync", False)
        return self

    @model_validator(mode="after")
    def _only_a_reader_prunes(self) -> "Device":
        """`prune` means nothing off a `books` node: a mirror's --delete is its mode (and
        `prune: false` must not look like it made a Replicate safe), an upstream is never
        written. Coerced for the reason above."""
        if self.sync_mode != "books":
            object.__setattr__(self, "prune", False)
        return self

    @field_validator("fs", mode="before")
    @classmethod
    def _normalise_fs(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().lower().lstrip(".")
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
        """Strictly "replicated to, with --delete". Never widen it to "CAS-shaped"
        (`cas_tree`) or "not a reader" (`is_selectable`): that is how /replicate would come
        back pointed at an upstream."""
        return self.sync_mode == "mirror"

    @property
    def is_upstream(self) -> bool:
        """A pull source, and never a transfer destination."""
        return self.sync_mode == "upstream"

    @property
    def dom_id(self) -> str:
        """The id, safe in a DOM id *and* a CSS selector; every `id=` and `#…` uses this.

        htmx's out-of-band swap builds its selector as `"#" + getAttribute("id")`, so the
        attribute itself must be selector-safe. `#scan-status-sigmaai.au` parses as an id
        plus the class `au`, matches nothing, and htmx then *does not send the request*:
        Scan on that node did nothing, with nothing in the log. URLs keep the real id, which
        is the key in every database.
        """
        return re.sub(r"[^A-Za-z0-9_-]", "-", self.id)

    @property
    def cas_tree(self) -> bool:
        """This node's tree *is* the CAS shape: a mirror or an upstream. Decides a scan's
        `-l`, `keep_links` and the extras dialog's expected vault -- miss one on an upstream
        and a full production library reads as an empty backlog."""
        return self.sync_mode in ("mirror", "upstream")

    @property
    def is_selectable(self) -> bool:
        """May this node be picked for a Library push? Positive on purpose, so a mode added
        later is excluded until someone opts it in (`not is_mirror` admitted `upstream`)."""
        return self.sync_mode == "books"

    @property
    def capacity_bytes(self) -> int | None:
        return parse_size(self.capacity)

    def excludes_with(self, defaults: Defaults) -> list[str]:
        own = self.excludes if self.excludes is not None else []
        return [*defaults.excludes, *own]

    def pull_excludes_with(self, fallback: Sequence[str]) -> list[str]:
        """What a pull from this node holds back: its own list, or `fallback`
        (config.PULL_EXCLUDES, passed in to avoid an import cycle). Replaces rather than
        extends, so whoever narrows one boundary sees the whole list they are choosing."""
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
        """Two ids must not fold to one `dom_id` (`sigmaai.au` and `sigmaai-au`), or every
        swap aimed at either hits whichever came first."""
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
