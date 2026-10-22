"""Network protection (Pi-hole) status: DNS checks, API stats, privacy, honesty."""

import asyncio
import logging
import socket
import struct
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app.dnsfilter import DnsFilterMonitor, build_query, lookup, parse_answer
from app.main import create_app
from conftest import FakeNetwork, make_settings

APP_PW = "app-pw-SECRET-123"


def reply(query: bytes, ips: list[str], rcode: int = 0) -> bytes:
    qid = struct.unpack("!H", query[:2])[0]
    question = query[12:]
    header = struct.pack("!HHHHHH", qid, 0x8180 | rcode, 1, len(ips), 0, 0)
    answers = b"".join(struct.pack("!HHHIH", 0xC00C, 1, 1, 60, 4) + socket.inet_aton(ip) for ip in ips)
    return header + question + answers


class FakeDns:
    """UDP DNS server on 127.0.0.1:<random>: example.com resolves, doubleclick.net -> 0.0.0.0."""

    def __init__(self, block=True, answer=True):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.block, self.answer = block, answer
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(512)
            except OSError:
                return
            if not self.answer:
                continue
            name = b"doubleclick" in data
            ips = (["0.0.0.0"] if self.block else ["142.250.1.1"]) if name else ["93.184.215.14"]
            self.sock.sendto(reply(data, ips), addr)

    def close(self):
        self.sock.close()


def test_query_and_reply_roundtrip():
    q = build_query("example.com", 4242)
    rc, ips = parse_answer(reply(q, ["1.2.3.4", "5.6.7.8"]), 4242)
    assert rc == 0 and ips == ["1.2.3.4", "5.6.7.8"]
    with pytest.raises(ValueError):
        parse_answer(reply(q, ["1.2.3.4"]), 1)            # wrong id
    rc, ips = parse_answer(reply(q, [], rcode=3), 4242)
    assert rc == 3 and ips == []


def test_lookup_against_fake_server():
    dns = FakeDns()
    try:
        assert lookup("example.com", "127.0.0.1", dns.port) == (0, ["93.184.215.14"])
        assert lookup("doubleclick.net", "127.0.0.1", dns.port) == (0, ["0.0.0.0"])
    finally:
        dns.close()


# ------------------------------------------------------------------ monitor

class PiholeApi:
    def __init__(self, password=APP_PW, expire_first=False):
        self.password, self.expire_first = password, expire_first
        self.calls, self.sids = [], 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.url.path == "/api/auth" and request.method == "POST":
            import json
            if json.loads(request.content)["password"] != self.password:
                return httpx.Response(401, json={"session": {"valid": False}})
            self.sids += 1
            return httpx.Response(200, json={"session": {"valid": True, "sid": f"sid{self.sids}"}})
        if request.url.path == "/api/auth" and request.method == "DELETE":
            return httpx.Response(204)
        sid = request.headers.get("X-FTL-SID")
        if not sid or (self.expire_first and sid == "sid1"):
            return httpx.Response(401, json={"error": {"key": "unauthorized"}})
        if request.url.path == "/api/stats/summary":
            return httpx.Response(200, json={
                "queries": {"total": 12034, "blocked": 1810, "percent_blocked": 15.0412},
                "clients": {"active": 3, "total": 4},
                "gravity": {"domains_being_blocked": 76543, "last_update": 1792700000}})
        if request.url.path == "/api/dns/blocking":
            return httpx.Response(200, json={"blocking": "enabled"})
        return httpx.Response(404)


def monitor(dns, api=None, password=APP_PW, service="running", router=("192.168.1.1",)):
    m = DnsFilterMonitor("http://127.0.0.1", password, transport=httpx.MockTransport(api or PiholeApi()),
                         dns_port=dns.port)

    async def svc():
        return service

    async def rdns():
        return list(router)
    m.service_state, m.router_dns = svc, rdns
    m.pi_address = staticmethod(lambda: "192.168.1.11")
    return m


def run(m, **kw):
    async def go():
        try:
            return await m.status(**kw)
        finally:
            await m.aclose()
    return asyncio.run(go())


def test_healthy_status_with_stats():
    dns = FakeDns()
    try:
        st = run(monitor(dns))
    finally:
        dns.close()
    assert st.service == "running" and st.dns_ok and st.blocking_ok and st.last_ok_at
    assert st.stats.total_queries == 12034 and st.stats.blocked_queries == 1810 and st.stats.percent_blocked == 15.0
    assert st.stats.domains_on_blocklists == 76543 and st.stats.blocking_enabled is True
    assert st.router_uses_pihole is False and st.problems == []


def test_router_pointing_at_pi_is_recognised():
    dns = FakeDns()
    try:
        assert run(monitor(dns, router=("192.168.1.11",))).router_uses_pihole is True
    finally:
        dns.close()


def test_no_password_means_no_stats_but_honest_note():
    dns, api = FakeDns(), PiholeApi()
    try:
        st = run(monitor(dns, api=api, password=None))
    finally:
        dns.close()
    assert st.stats is None and "AURA_PIHOLE_APP_PASSWORD" in st.stats_note and api.calls == []
    assert st.dns_ok and st.blocking_ok


def test_wrong_password():
    dns = FakeDns()
    try:
        st = run(monitor(dns, password="nope"))
    finally:
        dns.close()
    assert st.stats is None and "rejected" in st.stats_note


def test_expired_session_is_renewed_once():
    dns, api = FakeDns(), PiholeApi(expire_first=True)
    try:
        st = run(monitor(dns, api=api))
    finally:
        dns.close()
    assert st.stats and api.sids == 2


def test_not_blocking_and_not_answering_are_reported():
    dns = FakeDns(block=False)
    try:
        st = run(monitor(dns))
    finally:
        dns.close()
    assert st.blocking_ok is False and any("not blocked" in p for p in st.problems)
    silent = FakeDns(answer=False)
    try:
        st = run(monitor(silent))
    finally:
        silent.close()
    assert st.dns_ok is False and st.last_ok_at is None and any("didn't answer" in p for p in st.problems)


def test_stopped_and_missing_service():
    dns = FakeDns()
    try:
        st = run(monitor(dns, service="stopped"))
        assert st.stats is None and any("isn't running" in p for p in st.problems)
        st = run(monitor(dns, service="not_installed"))
        assert not st.installed and st.dns_ok is None and st.problems == ["Pi-hole is not installed."]
    finally:
        dns.close()


def test_results_are_cached_briefly():
    dns, api = FakeDns(), PiholeApi()
    m = monitor(dns, api=api)

    async def go():
        await m.status()
        await m.status()
        await m.status(force=True)
        await m.aclose()
    try:
        asyncio.run(go())
    finally:
        dns.close()
    assert api.calls.count(("GET", "/api/stats/summary")) == 2


def test_api_endpoint_never_exposes_the_password(tmp_path, caplog):
    dns = FakeDns()
    try:
        m = monitor(dns)
        app = create_app(make_settings(tmp_path, edit_access="lan", pihole_app_password=APP_PW),
                         transport=FakeNetwork().transport, dnsfilter=m)
        c = TestClient(app, base_url="http://localhost", client=("192.168.1.3", 5000))
        with caplog.at_level(logging.DEBUG):
            r = c.get("/api/system/dns-filter?refresh=true")
            body = r.text + c.get("/api/settings").text + c.get("/api/config").text
    finally:
        dns.close()
    assert r.status_code == 200 and r.json()["stats"]["total_queries"] == 12034
    assert APP_PW not in body and APP_PW not in caplog.text
    # read-only: no way to change Pi-hole through AURA
    for method in ("post", "put", "patch", "delete"):
        assert getattr(c, method)("/api/system/dns-filter").status_code == 405
