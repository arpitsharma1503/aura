"""Work out the URL phones should use to reach this Pi on the home network.

The Pi's IP can change (DHCP), so it is looked up on every request rather
than stored. ``AURA_PUBLIC_URL`` overrides it, e.g. once a DHCP reservation or
local DNS name is set up.
"""

from __future__ import annotations

import ipaddress
import socket


def lan_ipv4() -> str | None:
    """The IPv4 address of the interface used for the local network."""
    # Connecting a UDP socket sends no packets; it only selects a route.
    for probe in ("192.168.0.1", "10.0.0.1", "172.16.0.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 9))
                ip = s.getsockname()[0]
        except OSError:
            continue
        addr = ipaddress.ip_address(ip)
        if addr.is_private and not addr.is_loopback:
            return ip
    return None


def share_url(public_url: str, host: str, port: int, path: str = "/groceries") -> tuple[str | None, str]:
    """Return (url, how) for the QR code. ``url`` is None if phones can't connect."""
    if public_url:
        return public_url.rstrip("/") + path, "configured"
    if host.startswith("127.") or host == "localhost":
        return None, "The server only listens on this Pi (AURA_HOST=127.0.0.1)"
    ip = lan_ipv4() if host in ("0.0.0.0", "") else host
    if not ip:
        return None, "The Pi has no home-network address right now"
    suffix = "" if port == 80 else f":{port}"
    return f"http://{ip}{suffix}{path}", "detected"


def mdns_url(port: int, path: str = "/groceries") -> str:
    suffix = "" if port == 80 else f":{port}"
    return f"http://{socket.gethostname()}.local{suffix}{path}"
