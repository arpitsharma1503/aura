"""Network protection status: is Pi-hole running, answering and blocking?

Read-only. AURA never changes Pi-hole; its admin page (with its own password)
is the place for that. Three independent checks:

- service:  systemd state of pihole-FTL (fixed argv, no shell)
- DNS:      real lookups sent to 127.0.0.1:53 (a normal domain must resolve,
            a known ad domain must come back as 0.0.0.0)
- stats:    Pi-hole's official REST API (/api/stats/summary), only when an
            app password is configured in .env; it never leaves this process

It also reports which DNS server the router's DHCP hands out, because devices
are filtered only when they actually use the Pi for DNS.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
import socket
import struct
import time
from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import BaseModel

log = logging.getLogger("aura.dnsfilter")

TEST_OK = "example.com"
TEST_BLOCKED = "doubleclick.net"          # on every mainstream ad list, incl. StevenBlack
CACHE_SECONDS = 30


class DnsStats(BaseModel):
    total_queries: int
    blocked_queries: int
    percent_blocked: float
    domains_on_blocklists: int | None = None
    active_clients: int | None = None
    blocklist_updated: datetime | None = None
    blocking_enabled: bool | None = None


class DnsFilterStatus(BaseModel):
    installed: bool
    service: Literal["running", "stopped", "failed", "not_installed", "unknown"]
    dns_ok: bool | None = None                   # a normal domain resolved via the Pi
    blocking_ok: bool | None = None              # the ad test domain was blocked
    last_ok_at: datetime | None = None           # last time both DNS checks passed
    checked_at: datetime
    stats: DnsStats | None = None
    stats_note: str | None = None                # why stats are missing
    router_dns: list[str] = []                   # DNS servers the router's DHCP gives out
    router_uses_pihole: bool | None = None
    pi_address: str | None = None
    problems: list[str] = []


# --------------------------------------------------------------------------- tiny DNS client

def build_query(name: str, qid: int) -> bytes:
    header = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)      # RD set, one question
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.rstrip(".").split(".")) + b"\0"
    return header + qname + struct.pack("!HH", 1, 1)              # A, IN


def _skip_name(data: bytes, i: int) -> int:
    while True:
        n = data[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:          # compression pointer
            return i + 2
        i += n + 1


def parse_answer(data: bytes, qid: int) -> tuple[int, list[str]]:
    """Returns (rcode, IPv4 answers). Raises ValueError on malformed data."""
    if len(data) < 12:
        raise ValueError("short DNS reply")
    rid, flags, qd, an, _ns, _ar = struct.unpack("!HHHHHH", data[:12])
    if rid != qid or not flags & 0x8000:
        raise ValueError("unexpected DNS reply")
    i = 12
    for _ in range(qd):
        i = _skip_name(data, i) + 4
    ips = []
    for _ in range(an):
        i = _skip_name(data, i)
        rtype, _cls, _ttl, rdlen = struct.unpack("!HHIH", data[i:i + 10])
        i += 10
        if rtype == 1 and rdlen == 4:
            ips.append(str(ipaddress.IPv4Address(data[i:i + 4])))
        i += rdlen
    return flags & 0x000F, ips


def lookup(name: str, server: str = "127.0.0.1", port: int = 53, timeout: float = 2.0) -> tuple[int, list[str]]:
    qid = secrets.randbelow(65536)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(build_query(name, qid), (server, port))
        while True:
            data, addr = s.recvfrom(4096)
            if addr[0] == server:
                return parse_answer(data, qid)


# --------------------------------------------------------------------------- service

class DnsFilterMonitor:
    def __init__(self, api_base: str = "http://127.0.0.1", app_password: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, dns_server: str = "127.0.0.1",
                 dns_port: int = 53) -> None:
        self.api_base = api_base.rstrip("/")
        self._password = app_password or None
        self.client = httpx.AsyncClient(base_url=self.api_base, timeout=5, transport=transport)
        self.dns_server, self.dns_port = dns_server, dns_port
        self._sid: str | None = None
        self._cache: tuple[float, DnsFilterStatus] | None = None
        self._last_ok: datetime | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._sid:
            try:
                await self.client.delete("/api/auth", headers={"X-FTL-SID": self._sid})
            except httpx.HTTPError:
                pass
        await self.client.aclose()

    # ----- pieces (overridable in tests) -----

    async def service_state(self) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "show", "pihole-FTL.service", "-p", "LoadState", "-p", "ActiveState",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except (OSError, asyncio.TimeoutError):
            return "unknown"
        props = dict(line.split("=", 1) for line in out.decode().splitlines() if "=" in line)
        if props.get("LoadState") == "not-found":
            return "not_installed"
        return {"active": "running", "failed": "failed", "inactive": "stopped",
                "activating": "running", "deactivating": "stopped"}.get(props.get("ActiveState", ""), "unknown")

    async def dns_checks(self) -> tuple[bool, bool | None]:
        def run():
            try:
                rc, ips = lookup(TEST_OK, self.dns_server, self.dns_port)
                ok = rc == 0 and any(ip != "0.0.0.0" for ip in ips)
            except (OSError, ValueError):
                return False, None
            try:
                rc, ips = lookup(TEST_BLOCKED, self.dns_server, self.dns_port)
                blocked = (rc == 0 and ips == ["0.0.0.0"]) or rc == 3      # 0.0.0.0 or NXDOMAIN
            except (OSError, ValueError):
                blocked = None
            return ok, blocked
        return await asyncio.to_thread(run)

    async def router_dns(self) -> list[str]:
        """DNS servers in the Pi's own DHCP lease = what the router gives every device."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "nmcli", "-t", "-g", "DHCP4.OPTION", "device", "show", "eth0",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except (OSError, asyncio.TimeoutError):
            return []
        for part in out.decode().replace("\n", "|").split("|"):
            part = part.strip()
            if part.startswith("domain_name_servers"):
                return part.split("=", 1)[1].split()
        return []

    @staticmethod
    def pi_address() -> str | None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))          # no packet is sent; picks the LAN source address
                return s.getsockname()[0]
        except OSError:
            return None

    async def _auth(self) -> str | None:
        r = await self.client.post("/api/auth", json={"password": self._password})
        if r.status_code != 200:
            raise PermissionError("Pi-hole rejected the app password")
        sid = r.json().get("session", {}).get("sid")
        if not sid:
            raise PermissionError("Pi-hole did not open a session")
        return sid

    async def _get(self, path: str) -> dict:
        for attempt in (1, 2):
            if not self._sid:
                self._sid = await self._auth()
            r = await self.client.get(path, headers={"X-FTL-SID": self._sid})
            if r.status_code == 401 and attempt == 1:
                self._sid = None                  # session expired: log in again once
                continue
            r.raise_for_status()
            return r.json()
        raise PermissionError("Pi-hole session could not be renewed")

    async def stats(self) -> tuple[DnsStats | None, str | None]:
        if not self._password:
            return None, ("Query statistics need a Pi-hole app password in .env "
                          "(AURA_PIHOLE_APP_PASSWORD). See README → Network protection.")
        try:
            s = await self._get("/api/stats/summary")
            try:
                blocking = (await self._get("/api/dns/blocking")).get("blocking")
            except (httpx.HTTPError, PermissionError):
                blocking = None
        except PermissionError as exc:
            return None, f"{exc}. Check AURA_PIHOLE_APP_PASSWORD."
        except (httpx.HTTPError, ValueError):
            return None, "Pi-hole's API didn't answer."
        q, g, c = s.get("queries", {}), s.get("gravity", {}), s.get("clients", {})
        updated = g.get("last_update")
        return DnsStats(
            total_queries=int(q.get("total", 0)), blocked_queries=int(q.get("blocked", 0)),
            percent_blocked=round(float(q.get("percent_blocked", 0.0)), 1),
            domains_on_blocklists=g.get("domains_being_blocked"), active_clients=c.get("active"),
            blocklist_updated=datetime.fromtimestamp(updated, UTC) if updated else None,
            blocking_enabled=None if blocking is None else blocking == "enabled",
        ), None

    # ----- combined -----

    async def status(self, force: bool = False) -> DnsFilterStatus:
        async with self._lock:
            if not force and self._cache and time.monotonic() - self._cache[0] < CACHE_SECONDS:
                return self._cache[1]
            now = datetime.now(UTC)
            service = await self.service_state()
            st = DnsFilterStatus(installed=service != "not_installed", service=service, checked_at=now,
                                 pi_address=self.pi_address())
            st.router_dns = await self.router_dns()
            if st.pi_address and st.router_dns:
                st.router_uses_pihole = st.pi_address in st.router_dns
            if service != "not_installed":
                st.dns_ok, st.blocking_ok = await self.dns_checks()
                if st.dns_ok and st.blocking_ok:
                    self._last_ok = now
                st.last_ok_at = self._last_ok
                if service == "running":
                    st.stats, st.stats_note = await self.stats()
                else:
                    st.stats_note = "Pi-hole isn't running."
            st.problems = self._problems(st)
            self._cache = (time.monotonic(), st)
            return st

    @staticmethod
    def _problems(st: DnsFilterStatus) -> list[str]:
        p = []
        if not st.installed:
            return ["Pi-hole is not installed."]
        if st.service != "running":
            p.append("The Pi-hole service isn't running. On the Pi: sudo systemctl start pihole-FTL "
                     "(devices pointed at the Pi have no DNS until it's back, or switch them to automatic DNS).")
        elif st.dns_ok is False:
            p.append("Pi-hole is running but didn't answer a test lookup. Check its upstream DNS and the internet "
                     "connection; see the admin page → System.")
        if st.dns_ok and st.blocking_ok is False:
            p.append("A known ad domain was not blocked: blocking may be paused, or the blocklists are empty "
                     "(admin page → Lists → Update gravity).")
        if st.stats and st.stats.blocking_enabled is False:
            p.append("Blocking is paused in Pi-hole.")
        return p
