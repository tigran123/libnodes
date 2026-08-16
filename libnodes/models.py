"""The devices.yaml schema, plus the YAML->line mapping the validation strip needs."""

from __future__ import annotations

import re
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
)

# devices.yaml is hand-edited, and YAML already yields native ints and bools for
# unquoted scalars. So a *string* reaching an int field means the user quoted it, and
# pydantic's default lax coercion would silently accept `port: "8022 "` — the exact
# mistake the design's validation strip is built to catch. Strict types make it an error.
Int = StrictInt
Bool = StrictBool

NodeType = Literal["kobo", "termux", "linux"]

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

    #: Can it store unix permissions? On FAT rsync's chmod fails on every run and it
    #: then counts every file as changed: measured on a real Android SD card, one
    #: directory reported 43 items needing work with perms on and 0 with them off.
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
    def capacity_bytes(self) -> int | None:
        return parse_size(self.capacity)

    @property
    def keep_free_bytes(self) -> int | None:
        return parse_size(self.keep_free)

    def excludes_with(self, defaults: Defaults) -> list[str]:
        own = self.excludes if self.excludes is not None else []
        return [*defaults.excludes, *own]

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
    "TYPE_SEEDS",
    "ValidationIssue",
    "parse_devices",
    "parse_size",
]
