"""Host telemetry for the rail footer.

The Devices/Library rails show uptime, load and library-disk usage; the Jobs rail swaps
to live net/cpu/temp. All of it comes from /proc and /sys, is cheap, and is cached for
a couple of seconds so a 10s poll plus an SSE stream cannot turn it into real work.

Every reader degrades to None rather than raising: not every host has a thermal_zone0
(pi5 does, an x86_64 workstation did not), and the interface name differs per machine.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

_CACHE_TTL = 2.0


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


@dataclass(frozen=True)
class HostStats:
    hostname: str
    uptime: float | None
    load: tuple[float, float, float] | None
    disk_used: int | None
    disk_total: int | None
    cpu_pct: float | None
    temp_c: float | None
    net_out: float | None  # bytes/sec

    @property
    def uptime_label(self) -> str:
        if self.uptime is None:
            return "—"
        days = int(self.uptime // 86400)
        if days:
            return f"up {days}d"
        hours = int(self.uptime // 3600)
        if hours:
            return f"up {hours}h"
        return f"up {int(self.uptime // 60)}m"

    @property
    def load_label(self) -> str:
        if self.load is None:
            return "—"
        return " ".join(f"{x:.2f}" for x in self.load)

    @property
    def disk_pct(self) -> float:
        if not self.disk_total:
            return 0.0
        return 100.0 * (self.disk_used or 0) / self.disk_total


class _Sampler:
    """Keeps the previous /proc counters so rates can be differenced."""

    def __init__(self) -> None:
        self._at = 0.0
        self._stats: HostStats | None = None
        self._prev_cpu: tuple[int, int] | None = None
        self._prev_net: tuple[float, int] | None = None

    def _cpu_pct(self) -> float | None:
        raw = _read("/proc/stat")
        if not raw:
            return None
        for line in raw.splitlines():
            if line.startswith("cpu "):
                parts = [int(x) for x in line.split()[1:]]
                idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
                total = sum(parts)
                prev = self._prev_cpu
                self._prev_cpu = (idle, total)
                if prev is None:
                    return None
                d_idle, d_total = idle - prev[0], total - prev[1]
                if d_total <= 0:
                    return None
                return max(0.0, min(100.0, 100.0 * (1 - d_idle / d_total)))
        return None

    def _temp_c(self) -> float | None:
        for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
            raw = _read(str(zone))
            if raw and raw.strip().isdigit():
                return int(raw.strip()) / 1000.0
        return None

    def _net_out(self) -> float | None:
        raw = _read("/proc/net/dev")
        if not raw:
            return None
        total = 0
        for line in raw.splitlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            if name == "lo" or not rest:
                continue
            fields = rest.split()
            if len(fields) >= 9:
                total += int(fields[8])  # tx bytes
        now = time.time()
        prev = self._prev_net
        self._prev_net = (now, total)
        if prev is None or now <= prev[0]:
            return None
        return max(0.0, (total - prev[1]) / (now - prev[0]))

    def sample(self, library_root: os.PathLike[str] | str) -> HostStats:
        now = time.time()
        if self._stats is not None and now - self._at < _CACHE_TTL:
            return self._stats

        uptime = None
        raw = _read("/proc/uptime")
        if raw:
            try:
                uptime = float(raw.split()[0])
            except (ValueError, IndexError):
                uptime = None

        try:
            load = os.getloadavg()
        except OSError:
            load = None

        disk_used = disk_total = None
        try:
            st = os.statvfs(library_root)
            disk_total = st.f_blocks * st.f_frsize
            disk_used = disk_total - st.f_bfree * st.f_frsize
        except OSError:
            pass

        self._stats = HostStats(
            hostname=socket.gethostname(),
            uptime=uptime,
            load=load,
            disk_used=disk_used,
            disk_total=disk_total,
            cpu_pct=self._cpu_pct(),
            temp_c=self._temp_c(),
            net_out=self._net_out(),
        )
        self._at = now
        return self._stats


_sampler = _Sampler()


def host_stats(library_root: os.PathLike[str] | str) -> HostStats:
    return _sampler.sample(library_root)


__all__ = ["HostStats", "host_stats"]
