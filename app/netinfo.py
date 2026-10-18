"""Network status and Wi-Fi management via NetworkManager (``nmcli``).

Security model
--------------
* Only a fixed set of nmcli operations exists here (status, scan, connect).
  Nothing from the browser is ever run as a command; values (SSID) are passed
  as separate argv items to ``nmcli`` with no shell.
* Wi-Fi passwords never appear on a command line, in logs or in responses.
  They are written to a private temporary file (mode 600, in the user's
  runtime directory) for ``nmcli connection up ... passwd-file`` and deleted
  immediately. NetworkManager stores the secret in its own root-only
  connection file, as it does for any Wi-Fi network.
* Connecting is only allowed to a network seen in a recent scan, with an
  explicit confirmation, one attempt at a time. A failed attempt removes the
  half-created profile so NetworkManager falls back to the previous network.
* If Wi-Fi is the only working connection, switching networks could cut off
  remote access (SSH and this dashboard). That requires an extra explicit
  acknowledgement, and the UI warns about it.
* Permission comes from NetworkManager's own policy: on Raspberry Pi OS a
  user in the netdev/sudo group with an active local (desktop) session may
  manage connections. If that isn't the case, nmcli says "not authorized" and
  we report it; nothing is escalated.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from .network import lan_ipv4

log = logging.getLogger("aura.network")

SCAN_COOLDOWN_S = 8
SCAN_VALID_S = 300
CONNECT_TIMEOUT_S = 45


def split_terse(line: str) -> list[str]:
    """Split one line of ``nmcli -t`` output (':' separated, '\\:' escaped)."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


class WifiNetwork(BaseModel):
    ssid: str
    signal: int
    security: str          # "" for open networks
    in_use: bool
    band: str | None = None
    saved: bool = False

    @property
    def open(self) -> bool:
        return self.security.strip() in ("", "--")

    @property
    def enterprise(self) -> bool:
        return "802.1X" in self.security


class InterfaceInfo(BaseModel):
    device: str
    type: str
    state: str
    connection: str | None = None
    ipv4: str | None = None


class NetworkStatus(BaseModel):
    available: bool
    hostname: str
    lan_ip: str | None
    state: str | None = None               # NetworkManager overall state
    connectivity: str | None = None        # full / limited / portal / none / unknown
    wifi_radio: str | None = None
    wifi_connected: bool = False
    wifi_ssid: str | None = None
    wifi_signal: int | None = None
    default_interface: str | None = None
    interfaces: list[InterfaceInfo] = []
    saved_wifi: list[str] = []
    can_manage: bool = False
    manage_reason: str | None = None
    message: str | None = None


class ConnectResult(BaseModel):
    ok: bool
    code: Literal["connected", "already_connected", "wrong_password", "not_found", "timeout",
                  "not_authorized", "failed", "invalid", "risky", "busy", "unsupported"]
    message: str
    ssid: str | None = None
    ipv4: str | None = None


class NetworkService:
    def __init__(self, nmcli: str | None = None, runtime_dir: str | None = None) -> None:
        self.nmcli = nmcli if nmcli is not None else shutil.which("nmcli")
        self.runtime_dir = runtime_dir or os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        self._scan_at = 0.0
        self._scan: dict[str, WifiNetwork] = {}
        self._lock = asyncio.Lock()

    # ----- plumbing -----

    async def _run(self, *args: str, timeout: float = 20) -> tuple[int, str, str]:
        if not self.nmcli:
            return 127, "", "nmcli not available"
        env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
        proc = await asyncio.create_subprocess_exec(
            self.nmcli, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL, env=env)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", "timed out"
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def _terse(self, *args: str, timeout: float = 20) -> list[list[str]]:
        rc, out, _ = await self._run("-t", *args, timeout=timeout)
        if rc != 0:
            return []
        return [split_terse(line) for line in out.splitlines() if line.strip()]

    async def _wifi_device(self) -> str | None:
        for f in await self._terse("-f", "DEVICE,TYPE", "device"):
            if len(f) >= 2 and f[1] == "wifi":
                return f[0]
        return None

    async def permissions(self) -> dict[str, str]:
        return {f[0]: f[1] for f in await self._terse("general", "permissions") if len(f) >= 2}

    # ----- status -----

    async def status(self) -> NetworkStatus:
        st = NetworkStatus(available=bool(self.nmcli), hostname=socket.gethostname(), lan_ip=lan_ipv4())
        if not self.nmcli:
            st.message = "NetworkManager (nmcli) isn't available on this system."
            return st
        general = await self._terse("-f", "STATE,CONNECTIVITY,WIFI", "general", "status")
        if general and len(general[0]) >= 3:
            st.state, st.connectivity, st.wifi_radio = general[0][:3]
        for f in await self._terse("-f", "DEVICE,TYPE,STATE,CONNECTION", "device"):
            if len(f) < 4 or f[1] not in ("ethernet", "wifi"):
                continue
            iface = InterfaceInfo(device=f[0], type=f[1], state=f[2], connection=f[3] or None)
            if iface.state == "connected":
                for g in await self._terse("-f", "IP4.ADDRESS", "device", "show", iface.device):
                    if len(g) >= 2 and g[1]:
                        iface.ipv4 = g[1].split("/")[0]
                        break
            st.interfaces.append(iface)
        wifi = next((i for i in st.interfaces if i.type == "wifi"), None)
        if wifi and wifi.state == "connected":
            st.wifi_connected = True
            for f in await self._terse("-f", "IN-USE,SSID,SIGNAL", "device", "wifi", "list",
                                       "ifname", wifi.device, "--rescan", "no"):
                if len(f) >= 3 and f[0] == "*":
                    st.wifi_ssid, st.wifi_signal = f[1], int(f[2]) if f[2].isdigit() else None
                    break
        st.saved_wifi = [f[0] for f in await self._terse("-f", "NAME,TYPE", "connection", "show")
                         if len(f) >= 2 and f[1] == "802-11-wireless"]
        st.default_interface = default_route_interface()
        perms = await self.permissions()
        allowed = perms.get("org.freedesktop.NetworkManager.network-control") == "yes" and \
            perms.get("org.freedesktop.NetworkManager.settings.modify.system") == "yes"
        st.can_manage = bool(wifi) and allowed
        if not wifi:
            st.manage_reason = "This Pi has no Wi-Fi adapter that NetworkManager can use."
        elif not allowed:
            st.manage_reason = ("NetworkManager doesn't allow AURA to change networks right now "
                                "(it needs the desktop auto-login session to be active).")
        return st

    # ----- scan -----

    async def scan(self) -> list[WifiNetwork]:
        dev = await self._wifi_device()
        if not dev:
            return []
        if time.monotonic() - self._scan_at < SCAN_COOLDOWN_S and self._scan:
            return sorted(self._scan.values(), key=lambda n: -n.signal)
        rows = await self._terse("-f", "IN-USE,SSID,SIGNAL,SECURITY,FREQ", "device", "wifi", "list",
                                 "ifname", dev, "--rescan", "yes", timeout=30)
        saved = await self._saved_ssids()
        found: dict[str, WifiNetwork] = {}
        for f in rows:
            if len(f) < 5 or not f[1]:
                continue                   # hidden networks have no SSID
            freq = f[4].split()[0] if f[4] else ""
            band = "5 GHz" if freq.startswith("5") else "6 GHz" if freq.startswith("6") else "2.4 GHz" if freq else None
            net = WifiNetwork(ssid=f[1], signal=int(f[2]) if f[2].isdigit() else 0,
                              security="" if f[3] in ("", "--") else f[3], in_use=f[0] == "*",
                              band=band, saved=f[1] in saved)
            prev = found.get(net.ssid)
            if prev is None or net.signal > prev.signal or net.in_use:
                found[net.ssid] = net
        self._scan, self._scan_at = found, time.monotonic()
        log.info("network: Wi-Fi scan found %d network(s)", len(found))
        return sorted(found.values(), key=lambda n: -n.signal)

    async def _saved_ssids(self) -> dict[str, str]:
        """SSID -> profile name for saved Wi-Fi connections."""
        out: dict[str, str] = {}
        for f in await self._terse("-f", "NAME,TYPE", "connection", "show"):
            if len(f) >= 2 and f[1] == "802-11-wireless":
                rc, ssid, _ = await self._run("-g", "802-11-wireless.ssid", "connection", "show", f[0])
                if rc == 0 and ssid.strip():
                    out.setdefault(ssid.strip(), f[0])
        return out

    # ----- connect -----

    @staticmethod
    def validate_password(net: WifiNetwork, password: str) -> str | None:
        if net.enterprise:
            return "This network uses enterprise (802.1X) sign-in, which AURA can't set up. Use the Raspberry Pi desktop instead."
        if "WEP" in net.security and "WPA" not in net.security:
            return "WEP networks are insecure and not supported."
        if net.open:
            return None if not password else "This network is open; it doesn't take a password."
        if not password:
            return "Enter the Wi-Fi password."
        if "\n" in password or "\r" in password:
            return "The password can't contain line breaks."
        if re.fullmatch(r"[0-9a-fA-F]{64}", password):
            return None
        if not (8 <= len(password) <= 63) or not all(32 <= ord(c) < 127 for c in password):
            return "WPA passwords are 8-63 standard characters."
        return None

    async def connect(self, ssid: str, password: str, *, allow_disconnect: bool = False) -> ConnectResult:
        if self._lock.locked():
            return ConnectResult(ok=False, code="busy", message="Another Wi-Fi change is in progress.")
        async with self._lock:
            return await self._connect(ssid, password, allow_disconnect)

    async def _connect(self, ssid: str, password: str, allow_disconnect: bool) -> ConnectResult:
        dev = await self._wifi_device()
        if not dev:
            return ConnectResult(ok=False, code="unsupported", message="No Wi-Fi adapter found.")
        net = self._scan.get(ssid) if time.monotonic() - self._scan_at < SCAN_VALID_S else None
        if net is None:
            return ConnectResult(ok=False, code="invalid", ssid=ssid,
                                 message="Scan for networks first, then choose one from the list.")
        status = await self.status()
        if status.wifi_connected and status.wifi_ssid == ssid:
            return ConnectResult(ok=True, code="already_connected", ssid=ssid,
                                 message=f"Already connected to {ssid}.")
        if not status.can_manage:
            return ConnectResult(ok=False, code="not_authorized", ssid=ssid,
                                 message=status.manage_reason or "Not allowed to change networks.")
        others = [i for i in status.interfaces if i.state == "connected" and i.device != dev]
        if status.wifi_connected and not others and not allow_disconnect:
            return ConnectResult(
                ok=False, code="risky", ssid=ssid,
                message=("Wi-Fi is this Pi's only connection. If the new network fails, you may lose "
                         "remote access until it reconnects to the old one. Confirm to continue."))
        saved = await self._saved_ssids()
        if not password and ssid in saved:
            return await self._activate(saved[ssid], dev, ssid, None, created=False)
        problem = self.validate_password(net, password)
        if problem:
            return ConnectResult(ok=False, code="invalid", ssid=ssid, message=problem)

        name = f"AURA Wi-Fi {int(time.time())}"
        add = ["connection", "add", "type", "wifi", "con-name", name, "ifname", dev,
               "ssid", ssid, "autoconnect", "yes"]
        if not net.open:
            key_mgmt = "sae" if "WPA3" in net.security and "WPA2" not in net.security else "wpa-psk"
            add += ["wifi-sec.key-mgmt", key_mgmt]
        rc, _, err = await self._run(*add)
        if rc != 0:
            return self._failure(err, ssid)
        result = await self._activate(name, dev, ssid, None if net.open else password, created=True)
        if result.ok:
            # Replace AURA's older profile(s) for the same network.
            for prof_ssid, prof_name in saved.items():
                if prof_ssid == ssid and prof_name.startswith("AURA Wi-Fi"):
                    await self._run("connection", "delete", prof_name)
        return result

    async def _activate(self, profile: str, dev: str, ssid: str, password: str | None, created: bool) -> ConnectResult:
        args = ["--wait", str(CONNECT_TIMEOUT_S), "connection", "up", profile, "ifname", dev]
        secret_file = None
        try:
            if password is not None:
                fd, secret_file = tempfile.mkstemp(prefix=".aura-wifi-", dir=self.runtime_dir)
                with os.fdopen(fd, "w") as fh:          # mkstemp creates it 0600
                    fh.write(f"802-11-wireless-security.psk:{password}\n")
                args += ["passwd-file", secret_file]
            log.info("network: connecting Wi-Fi to %r", ssid)
            rc, out, err = await self._run(*args, timeout=CONNECT_TIMEOUT_S + 10)
        finally:
            if secret_file:
                Path(secret_file).unlink(missing_ok=True)
        if rc == 0:
            status = await self.status()
            wifi = next((i for i in status.interfaces if i.device == dev), None)
            log.info("network: Wi-Fi connected to %r", ssid)
            return ConnectResult(ok=True, code="connected", ssid=ssid, ipv4=wifi.ipv4 if wifi else None,
                                 message=f"Connected to {ssid}.")
        if created:
            await self._run("connection", "delete", profile)   # fall back to the previous network
        return self._failure(err or out, ssid)

    @staticmethod
    def _failure(err: str, ssid: str) -> ConnectResult:
        e = err.lower()
        log.warning("network: Wi-Fi connection to %r failed: %s", ssid, err.strip().splitlines()[-1:] or "")
        if "secrets were required" in e or "802-1x supplicant" in e or "psk" in e and "invalid" in e:
            return ConnectResult(ok=False, code="wrong_password", ssid=ssid,
                                 message="Couldn't connect: the password seems to be wrong.")
        if "no network with ssid" in e or "not found" in e:
            return ConnectResult(ok=False, code="not_found", ssid=ssid, message=f"{ssid} is no longer in range.")
        if "timed out" in e or "timeout" in e:
            return ConnectResult(ok=False, code="timeout", ssid=ssid,
                                 message="The network didn't respond in time. Check the signal and try again.")
        if "not authorized" in e or "insufficient privileges" in e:
            return ConnectResult(ok=False, code="not_authorized", ssid=ssid,
                                 message="NetworkManager refused the change (not authorized).")
        return ConnectResult(ok=False, code="failed", ssid=ssid, message="Couldn't connect to that network.")


def default_route_interface() -> str | None:
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            parts = line.split()
            if len(parts) > 2 and parts[1] == "00000000":
                return parts[0]
    except OSError:
        pass
    return None
