"""Read-only system information for the Settings Center.

Only reads well-known, non-sensitive sources (/proc, /etc/os-release, the
thermal zone). No credentials, keys, environment variables or file paths
beyond the data directory's free space are reported.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from . import __version__


class SystemInfo(BaseModel):
    hostname: str
    os: str
    kernel: str
    model: str | None
    python: str
    app_version: str
    app_started_at: datetime
    system_uptime_s: int | None
    disk_total_gb: float | None
    disk_free_gb: float | None
    memory_total_mb: int | None
    memory_available_mb: int | None
    cpu_temperature_c: float | None
    load_1m: float | None
    service: str


def _os_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip("\x00\n ")
    except OSError:
        return None


def collect(data_dir: Path, started_at: datetime) -> SystemInfo:
    meminfo = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        if v.strip().endswith("kB"):
            meminfo[k] = int(v.split()[0]) // 1024
    try:
        du = shutil.disk_usage(data_dir)
        disk_total, disk_free = round(du.total / 1e9, 1), round(du.free / 1e9, 1)
    except OSError:
        disk_total = disk_free = None
    up = _read("/proc/uptime")
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = None
    # systemd sets INVOCATION_ID for every unit it starts.
    service = "systemd user service (aura.service)" if os.environ.get("INVOCATION_ID") else "started manually"
    return SystemInfo(
        hostname=socket.gethostname(),
        os=_os_name(),
        kernel=platform.release(),
        model=_read("/proc/device-tree/model"),
        python=sys.version.split()[0],
        app_version=__version__,
        app_started_at=started_at,
        system_uptime_s=int(float(up.split()[0])) if up else None,
        disk_total_gb=disk_total,
        disk_free_gb=disk_free,
        memory_total_mb=meminfo.get("MemTotal"),
        memory_available_mb=meminfo.get("MemAvailable"),
        cpu_temperature_c=round(int(temp) / 1000, 1) if temp and temp.isdigit() else None,
        load_1m=round(load, 2) if load is not None else None,
        service=service,
    )


def now_utc() -> datetime:
    return datetime.now(UTC)
