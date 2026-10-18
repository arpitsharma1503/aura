"""Home-network-only access controls.

Three independent checks, all applied before any route runs:

1. Client address must be loopback, private (RFC 1918 / ULA) or link-local.
   This is defence in depth on top of binding to the LAN; it does not replace
   a router that blocks inbound traffic.
2. Host header must be a private IP literal or an allow-listed hostname.
   This blocks DNS-rebinding attacks from malicious public websites.
3. State-changing requests sent by a browser must come from this same origin.
"""

from __future__ import annotations

import ipaddress

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    ),
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


def is_local_address(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local or (ip.is_private and not ip.is_unspecified)


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _host_without_port(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):  # [ipv6]:port
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class LanOnlyMiddleware:
    def __init__(self, app: ASGIApp, allowed_hosts: list[str], lan_only: bool = True) -> None:
        self.app = app
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.lan_only = lan_only

    def _host_ok(self, host_header: str) -> bool:
        host = _host_without_port(host_header)
        return host in self.allowed_hosts or is_local_address(host)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        if self.lan_only and not is_local_address(client[0] if client else None):
            await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        host_header = headers.get(b"host", b"").decode("latin-1")
        if not self._host_ok(host_header):
            await PlainTextResponse("Invalid host", status_code=400)(scope, receive, send)
            return

        # Cross-site request protection: a browser always sends Origin on
        # state-changing fetches; it must be this same server.
        origin = headers.get(b"origin")
        if scope.get("method") in UNSAFE_METHODS and origin is not None:
            origin_host = origin.decode("latin-1").split("://", 1)[-1].lower()
            if origin_host != host_header.strip().lower():
                await PlainTextResponse("Cross-site request blocked", status_code=403)(scope, receive, send)
                return

        async def send_with_headers(message):  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                existing = message.setdefault("headers", [])
                for k, v in SECURITY_HEADERS.items():
                    existing.append((k.encode(), v.encode()))
            await send(message)

        await self.app(scope, receive, send_with_headers)
