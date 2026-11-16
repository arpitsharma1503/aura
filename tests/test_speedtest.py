"""Internet speed pill: the Pi times a small download (and a few empty requests for ping)
from Cloudflare's speed test every few minutes. Simulated network, simulated timer."""

import asyncio
from datetime import datetime

import httpx

from app.speedtest import SpeedMonitor
from conftest import make_client

IST_NOON = datetime.fromisoformat("2026-11-03T12:00:00+05:30")


def timer(*values):
    it = iter(values)
    return lambda: next(it)


def monitor(handler, times, minutes=10):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SpeedMonitor(client, every_minutes=minutes, sample_bytes=1_000_000,
                        now=lambda: IST_NOON, timer=timer(*times), retry_seconds=0)


def test_speed_and_ping_are_measured():
    seen = []

    def handler(request):
        seen.append(request)
        n = int(request.url.params["bytes"])
        return httpx.Response(200, content=b"\0" * n)
    # three pings (40, 12, 20 ms), then 1 MB in 0.1 s = 80 Mbps
    m = monitor(handler, [0, 0.040, 1, 1.012, 2, 2.020, 10, 10.1])
    asyncio.run(m.measure())
    v = m.view()
    assert v["status"] == "ok" and v["down_mbps"] == 80.0 and v["ping_ms"] == 12
    assert v["measured_at"] == IST_NOON.isoformat() and v["every_minutes"] == 10
    assert all(r.url.host == "speed.cloudflare.com" for r in seen)
    assert [r.url.params["bytes"] for r in seen] == ["0", "0", "0", "1000000"]


def test_one_blip_is_retried_not_called_offline():
    """A single timeout (seen live: one ConnectTimeout while the internet was fine) is retried."""
    answers = ["down", "ok", "ok", "ok", "ok"]

    def handler(request):
        if answers.pop(0) == "down":
            raise httpx.ConnectTimeout("")
        return httpx.Response(200, content=b"\0" * int(request.url.params["bytes"]))
    m = monitor(handler, [0, 1, 1.01, 2, 2.01, 3, 3.01, 10, 10.1])
    asyncio.run(m.measure())
    assert m.view()["status"] == "ok" and m.view()["down_mbps"] == 80.0


def test_offline_only_after_every_try_fails_and_keeps_the_last_reading():
    fail = {"on": False}

    def handler(request):
        if fail["on"]:
            raise httpx.ConnectError("no route")
        return httpx.Response(200, content=b"\0" * int(request.url.params["bytes"]))
    m = monitor(handler, [0, 0.01, 1, 1.01, 2, 2.01, 10, 10.1] + [20, 21, 22])
    asyncio.run(m.measure())
    fail["on"] = True
    asyncio.run(m.measure())
    v = m.view()
    assert v["status"] == "offline" and v["down_mbps"] == 80.0 and "no internet" in v["message"].lower()


def test_turned_off_and_waiting_states():
    m = monitor(lambda r: httpx.Response(500), [], minutes=0)
    assert m.view()["status"] == "off"
    assert monitor(lambda r: httpx.Response(500), []).view()["status"] == "waiting"


def test_speed_api(tmp_path):
    c = make_client(tmp_path)
    v = c.get("/api/system/speed").json()
    assert v["status"] in ("waiting", "off") and v["down_mbps"] is None


def ok_handler(request):
    return httpx.Response(200, content=b"\0" * int(request.url.params["bytes"]))


def test_readings_are_kept_for_the_trend_line_with_plain_words():
    m = monitor(ok_handler, [0, 0.040, 1, 1.012, 2, 2.020, 10, 10.1])
    asyncio.run(m.measure())
    v = m.view()
    assert v["history"] == [{"at": IST_NOON.isoformat(), "down_mbps": 80.0, "ping_ms": 12}]
    assert v["quality"]["level"] == "good" and v["checking"] is False


def test_quality_words():
    from app.speedtest import quality
    assert quality(4.5, 91)["label"] == "Slow"
    assert quality(12, 40)["label"] == "Okay"
    assert quality(92.4, 18)["label"] == "Fast"


def test_test_now_runs_one_check_and_not_back_to_back():
    m = monitor(ok_handler, [0, 0.040, 1, 1.012, 2, 2.020, 10, 10.1])

    async def go():
        assert m.check_now() is True
        assert m.check_now() is False          # one at a time
        await m._manual
        assert m.check_now() is False          # just ran (under 60 s ago)
    asyncio.run(go())
    assert m.view()["down_mbps"] == 80.0


def test_test_now_route(tmp_path):
    with make_client(tmp_path, speedtest_minutes=0) as c:
        assert c.post("/api/system/speed/check").status_code == 409     # checks turned off


def test_the_trend_line_survives_a_restart(tmp_path):
    path = tmp_path / "speed_history.json"
    client = httpx.AsyncClient(transport=httpx.MockTransport(ok_handler))
    m = SpeedMonitor(client, sample_bytes=1_000_000, now=lambda: IST_NOON,
                     timer=timer(0, 0.040, 1, 1.012, 2, 2.020, 10, 10.1), retry_seconds=0, history_path=path)
    asyncio.run(m.measure())
    again = SpeedMonitor(client, history_path=path)                     # the restart
    assert again.view()["history"] == m.view()["history"]
    assert again.view()["down_mbps"] == 80.0 and again.view()["status"] == "ok"
