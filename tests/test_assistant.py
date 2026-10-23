"""Assistant: intent routing, grounded answers, safe failures, API and privacy."""

import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agenda import AgendaResponse, DayAgenda, FeedStatus, Occurrence
from app.assistant import TOOLS, Assistant, Sources, route
from app.commute import CommuteStatus, Recommendation, SlotView
from app.groceries import GroceryList, Item
from app.main import create_app
from app.weather import MockWeatherProvider, WeatherResponse
from conftest import FakeNetwork, make_settings

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 19, 8, 0, tzinfo=TZ)          # Monday 8 AM


# ---------------------------------------------------------------- fakes

def weather(status="ok", rain=(10, 20, 70, 30), mock=False, raining=False) -> WeatherResponse:
    snap = asyncio.run(MockWeatherProvider().fetch(SimpleNamespace(city="Bengaluru"), "Asia/Kolkata"))
    snap.is_mock = mock
    snap.city = "Bengaluru"
    snap.fetched_at = NOW - timedelta(minutes=5 if status == "ok" else 180)
    snap.current.temperature_c, snap.current.feels_like_c = 24.0, 27.0
    snap.current.description = "Partly cloudy"
    snap.current.precipitation_mm = 1.0 if raining else 0.0
    snap.high_c, snap.low_c = 29.0, 20.0
    snap.air_quality = None
    for i, h in enumerate(snap.hourly):
        h.time = NOW.replace(minute=0) + timedelta(hours=i)
        h.precipitation_chance_pct = rain[i] if i < len(rain) else 0
    return WeatherResponse(status=status, is_mock=mock, data=snap)


def commute(state="active", dep_hour=9, dep_min=15, leave_now=False, rec=True) -> CommuteStatus:
    dep = NOW.replace(hour=dep_hour, minute=dep_min)
    r = Recommendation(departure_at=dep, arrival_at=dep + timedelta(minutes=24), duration_min=24,
                       traffic_level="light", delay_min=2, basis="forecast", basis_label="Forecast",
                       leave_now=leave_now) if rec else None
    live = SlotView(departure_at=NOW, duration_min=41, traffic_level="heavy")
    msgs = {"unconfigured": "Live traffic needs a free TomTom API key (AURA_TOMTOM_API_KEY).",
            "off_day": "Saturday: no commute monitoring. Next check Monday 7 AM."}
    return CommuteStatus(state=state, message=msgs.get(state, "Watching traffic 7 AM–11 AM."), configured=state != "unconfigured",
                         provider="TomTom", timezone="Asia/Kolkata", home_label="Home", office_label="Office",
                         recommendation=r, live=live if state == "active" else None)


def occ(title, kind="event", day=None, hour=None, cals=("Personal",)):
    day = day or NOW.date()
    start = datetime.combine(day, datetime.min.time(), TZ).replace(hour=hour) if hour is not None else None
    return Occurrence(title=title, start=start, end=start + timedelta(hours=1) if start else None, first_day=day,
                      last_day=day, all_day=hour is None, kind=kind, calendars=list(cals))


def agenda(status="ok", days=None) -> AgendaResponse:
    if days is None:
        days = [DayAgenda(date=NOW.date(), items=[occ("Rohan's birthday", "birthday"), occ("Dentist", hour=18)]),
                DayAgenda(date=NOW.date() + timedelta(days=1), items=[occ("Dussehra", "holiday", NOW.date() + timedelta(days=1))])]
    return AgendaResponse(status=status, timezone="Asia/Kolkata", today=NOW.date(), days=days,
                          feeds=[FeedStatus(label="Personal", ok=status == "ok")])


def groceries(names=("Milk", "Eggs"), purchased=("Bread",)) -> GroceryList:
    items = []
    for i, (n, p) in enumerate([(n, False) for n in names] + [(n, True) for n in purchased], start=1):
        items.append(Item(id=i, name=n, quantity="2 L" if n == "Milk" else None, category="other", category_label="Other",
                          notes=None, purchased=p, created_at=NOW, updated_at=NOW, purchased_at=NOW if p else None, version=1))
    return GroceryList(status="ok" if items else "empty", items=items, remaining=len(names))


def sources(w=None, c=None, a=None, g=None, **kw):
    w = w if w is not None else weather()

    async def wx():
        return w
    return Sources(weather=wx, commute=lambda: c if c is not None else commute(),
                   calendar=lambda days: a if a is not None else agenda(),
                   groceries=lambda: g if g is not None else groceries(),
                   timezone=lambda: "Asia/Kolkata", now=lambda tz: NOW, **kw)


def ask(q, **kw):
    return asyncio.run(Assistant(sources(**kw)).ask(q)).answer


# ---------------------------------------------------------------- routing

@pytest.mark.parametrize("q,intent", [
    ("What's the weather today?", "weather"),
    ("How hot is it outside", "weather"),
    ("Will it rain before I leave?", "rain"),
    ("Do I need an umbrella", "rain"),
    ("How is my commute to the office?", "commute"),
    ("When should I leave for work", "commute"),
    ("What is on my calendar today?", "calendar"),
    ("Any birthdays this week?", "calendar"),
    ("What groceries do I need?", "groceries"),
    ("What's on the shopping list", "groceries"),
    ("Summarize my day.", "summary"),
    ("Aura, give me a briefing", "summary"),
    ("What devices are connected to my home network?", "network"),
    ("What time is it", "time"),
    ("What can you do?", "help"),
    ("Delete all my groceries", "action"),
    ("Turn off the Wi-Fi", "action"),
    ("Restart the Pi", "action"),
    ("Hello", "greeting"),
    ("Hey.", "greeting"),
    ("Bye, are you listening? Hello?", "greeting"),
    ("Good morning", "greeting"),
    ("Hey, what's the weather today?", "weather"),
    ("Good morning, summarize my day", "summary"),
])
def test_routing(q, intent):
    tool = route(q)
    assert tool is not None and tool.name == intent, (q, tool and tool.name)


def test_unknown_requests_fail_safely():
    for q in ["Tell me a joke about pirates", "rm -rf /", "Who won the cricket match", "sudo reboot now please"]:
        a = ask(q)
        assert not a.ok and a.intent in ("unknown", "action")
        assert "can't" in a.text


def test_tools_are_a_fixed_read_only_allowlist():
    assert {t.name for t in TOOLS} == {"action", "help", "greeting", "network", "summary", "rain", "calendar",
                                        "commute", "groceries", "weather", "time"}


# ---------------------------------------------------------------- grounded answers

def test_weather_answer_uses_real_numbers():
    a = ask("What's the weather today?")
    assert a.ok and "24°" in a.text and "Bengaluru" in a.text and "feeling like 27°" in a.text
    assert "high is 29°" in a.text and "low 20°" in a.text and "up to 70%" in a.text


def test_weather_respects_fahrenheit():
    a = ask("weather", temperature_unit=lambda: "fahrenheit")
    assert "75°F" in a.text and "84°F" in a.text


def test_weather_unavailable_is_honest():
    a = ask("What's the weather?", w=WeatherResponse(status="unavailable", reason="no_location", message="Set your home location."))
    assert not a.ok and "don't have weather data" in a.text and "°" not in a.text


def test_stale_and_sample_weather_are_labelled():
    assert "isn't reachable" in ask("weather", w=weather(status="stale")).text
    assert "sample data" in ask("weather", w=weather(mock=True)).text


def test_rain_before_leaving_uses_commute_departure():
    a = ask("Will it rain before I leave?")                      # leave 9:15; 8 AM 10%, 9 AM 20%
    assert a.ok and "before you leave at 9:15 AM" in a.text and "unlikely" in a.text and "20%" in a.text
    assert a.sources == ["weather", "commute"]
    late = ask("Will it rain before I leave?", c=commute(dep_hour=10, dep_min=30))   # includes 10 AM 70%
    assert "likely" in late.text and "70%" in late.text and "umbrella" in late.text


def test_rain_without_commute_uses_travel_window():
    a = ask("will it rain", c=commute(state="unconfigured", rec=False))
    assert "in the next 6 hours" in a.text and "70%" in a.text


def test_rain_mentions_current_rain():
    assert ask("umbrella?", w=weather(raining=True)).text.startswith("It's raining now.")


def test_commute_answer():
    a = ask("How is my commute to the office?")
    assert a.ok and "9:15 AM" in a.text and "24 minutes" in a.text and "light traffic" in a.text
    assert "9:39 AM" in a.text and "41 minutes" in a.text and "forecast" in a.text


def test_commute_unconfigured_does_not_invent():
    a = ask("traffic to office", c=commute(state="unconfigured", rec=False))
    assert not a.ok and "TomTom" in a.text and "minutes" not in a.text


def test_commute_off_day():
    a = ask("commute", c=commute(state="off_day", rec=False))
    assert "no commute monitoring" in a.text and "minutes" not in a.text


def test_calendar_today_tomorrow_and_birthdays():
    today = ask("What is on my calendar today?")
    assert today.ok and "Rohan's birthday (birthday)" in today.text and "Dentist at 6 PM" in today.text
    assert "Dussehra" not in today.text
    tmr = ask("anything on my calendar tomorrow")
    assert "Dussehra (public holiday)" in tmr.text and "Dentist" not in tmr.text
    births = ask("any birthdays this week")
    assert "Rohan" in births.text and "Dentist" not in births.text and "Dussehra" not in births.text


def test_calendar_empty_and_unconfigured():
    assert "nothing on your calendars" in ask("calendar today", a=agenda(days=[])).text
    a = ask("calendar", a=AgendaResponse(status="unconfigured", timezone="Asia/Kolkata", today=NOW.date()))
    assert not a.ok and "No calendar is connected" in a.text
    partial = ask("calendar today", a=agenda(status="partial"))
    assert "may be incomplete" in partial.text


def test_groceries_answer_only_lists_needed_items():
    a = ask("What groceries do I need?")
    assert a.ok and "2 items" in a.text and "Milk (2 L)" in a.text and "Eggs" in a.text and "Bread" not in a.text
    assert "empty" in ask("groceries", g=groceries(names=(), purchased=())).text


def test_summary_combines_sources():
    a = ask("Summarize my day.")
    assert a.ok and a.text.startswith("Good morning. It's Monday, 19 October.")
    for bit in ("24°", "9:15 AM", "Rohan's birthday", "Milk"):
        assert bit in a.text
    assert set(a.sources) == {"weather", "commute", "calendar", "groceries"}


def test_summary_skips_unconfigured_commute():
    a = ask("summarize my day", c=commute(state="unconfigured", rec=False))
    assert "TomTom" not in a.text and a.ok


def test_network_question_is_honest():
    a = ask("What devices are connected to my home network?")
    assert not a.ok and "can't list devices" in a.text


def test_broken_source_does_not_crash():
    def boom():
        raise RuntimeError("db locked")
    a = asyncio.run(Assistant(Sources(weather=None, commute=boom, calendar=None, groceries=None,
                                      timezone=lambda: "Asia/Kolkata", now=lambda tz: NOW)).ask("commute")).answer
    assert not a.ok and "went wrong" in a.text


def test_transcript_is_bounded_and_clearable():
    asst = Assistant(sources())
    for i in range(25):
        asyncio.run(asst.ask(f"what time is it {i}"))
    assert len(asst.transcript()) == 20
    asst.clear()
    assert asst.transcript() == []


def test_question_length_is_capped():
    turn = asyncio.run(Assistant(sources()).ask("weather " * 200))
    assert len(turn.question) <= 300


# ---------------------------------------------------------------- API (fake voice worker)

class FakeWorker:
    def __init__(self, text="what groceries do I need", record=None, healthy=True):
        self.text, self.record, self.healthy = text, record, healthy
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if not self.healthy:
            raise httpx.ConnectError("down", request=request)
        p = request.url.path
        if p == "/health":
            return httpx.Response(200, json={"stt": {"state": "ready", "model": "base.en", "error": None},
                                             "tts": {"state": "ready", "voice": "v", "error": None},
                                             "mic": {"present": True, "device": "hw", "busy": False}, "clips_in_memory": 0})
        if p == "/record":
            return httpx.Response(200, json=self.record or {"clip_id": "c1", "reason": "done", "seconds": 2.0})
        if p.startswith("/transcribe/"):
            return httpx.Response(200, json={"text": self.text, "stt_ms": 1500})
        if p == "/speak":
            body = json.loads(request.content)
            assert len(body["text"]) <= 1200
            return httpx.Response(200, content=b"RIFF....WAVEfmt ", headers={"content-type": "audio/wav"})
        if p in ("/stop", "/cancel"):
            return httpx.Response(200, json={})
        return httpx.Response(404)


def api(tmp_path, worker=None, client_ip="127.0.0.1", **overrides):
    worker = worker or FakeWorker()
    app = create_app(make_settings(tmp_path, edit_access="lan", **overrides),
                     transport=FakeNetwork().transport, voice_transport=httpx.MockTransport(worker))
    return TestClient(app, base_url="http://localhost", client=(client_ip, 50000)), worker


def test_status_endpoint(tmp_path):
    c, _ = api(tmp_path)
    body = c.get("/api/assistant").json()
    assert body["enabled"] and body["mic_allowed"] and body["voice_available"] and body["transcript"] == []
    phone, _ = api(tmp_path, client_ip="192.168.1.3")
    assert phone.get("/api/assistant").json()["mic_allowed"] is False


def test_voice_listen_flow(tmp_path):
    c, w = api(tmp_path)
    r = c.post("/api/assistant/listen")
    assert r.status_code == 200, r.text
    turn = r.json()
    assert turn["source"] == "voice" and turn["question"] == "what groceries do I need"
    assert turn["answer"]["intent"] == "groceries" and turn["timings_ms"]["transcribe"] == 1500
    assert w.calls[-2:] == ["/record", "/transcribe/c1"]
    assert c.app.state.voice.state == "answered"
    assert c.get("/api/assistant").json()["transcript"][0]["id"] == turn["id"]


def test_microphone_only_from_entrance_screen(tmp_path):
    phone, w = api(tmp_path, client_ip="192.168.1.3")
    for path in ("/api/assistant/listen", "/api/assistant/stop", "/api/assistant/cancel"):
        r = phone.post(path)
        assert r.status_code == 403 and "entrance screen" in r.json()["detail"]
    assert "/record" not in w.calls
    # ...but a phone can still type a question
    assert phone.post("/api/assistant/ask", json={"text": "what time is it"}).json()["answer"]["intent"] == "time"


def test_no_speech_and_empty_transcript_errors(tmp_path):
    c, _ = api(tmp_path, FakeWorker(record={"clip_id": None, "reason": "no_speech", "seconds": 5}))
    r = c.post("/api/assistant/listen")
    assert r.status_code == 422 and "didn't hear" in r.json()["detail"] and r.json()["retry"]
    assert c.app.state.voice.state == "error"
    c2, _ = api(tmp_path, FakeWorker(text="  "))
    assert "couldn't make out" in c2.post("/api/assistant/listen").json()["detail"]


def test_cancelled_listen_returns_nothing(tmp_path):
    c, _ = api(tmp_path, FakeWorker(record={"clip_id": None, "reason": "cancelled", "seconds": 1}))
    r = c.post("/api/assistant/listen")
    assert r.status_code == 200 and r.json() is None and c.app.state.voice.state == "idle"


def test_worker_down_is_reported(tmp_path):
    c, _ = api(tmp_path, FakeWorker(healthy=False))
    body = c.get("/api/assistant").json()
    assert body["voice_available"] is False
    r = c.post("/api/assistant/listen")
    assert r.status_code == 503 and "aura-voice" in r.json()["detail"]
    # typed questions keep working without the voice service
    assert c.post("/api/assistant/ask", json={"text": "groceries"}).status_code == 200


def test_ask_validation_and_speech(tmp_path):
    c, w = api(tmp_path)
    assert c.post("/api/assistant/ask", json={"text": ""}).status_code == 422
    assert c.post("/api/assistant/ask", json={"text": "x" * 301}).status_code == 422
    turn = c.post("/api/assistant/ask", json={"text": "what time is it"}).json()
    r = c.get(f"/api/assistant/speech/{turn['id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert c.get("/api/assistant/speech/999").status_code == 404


def test_clear_transcript(tmp_path):
    c, _ = api(tmp_path)
    c.post("/api/assistant/ask", json={"text": "what time is it"})
    assert c.delete("/api/assistant/transcript").status_code == 204
    assert c.get("/api/assistant").json()["transcript"] == []


def test_assistant_can_be_turned_off(tmp_path):
    c, w = api(tmp_path)
    assert c.patch("/api/settings/assistant", json={"enabled": False}).status_code == 200
    assert c.post("/api/assistant/ask", json={"text": "weather"}).status_code == 403
    assert c.post("/api/assistant/listen").status_code == 403
    assert "/record" not in w.calls
    c.patch("/api/settings/assistant", json={"enabled": True, "spoken_replies": False})
    turn = c.post("/api/assistant/ask", json={"text": "what time is it"}).json()
    assert c.get(f"/api/assistant/speech/{turn['id']}").status_code == 404


def test_settings_validation(tmp_path):
    c, _ = api(tmp_path)
    assert c.patch("/api/settings/assistant", json={"max_listen_seconds": 60}).status_code == 422
    info = c.get("/api/settings").json()
    assert info["values"]["assistant"] == {"enabled": True, "spoken_replies": True, "max_listen_seconds": 10,
                                           "mic_from_lan": False}
    assert info["info"]["assistant"]["available"] is True


def test_transcript_never_written_to_disk(tmp_path):
    c, _ = api(tmp_path)
    c.post("/api/assistant/ask", json={"text": "what groceries do I need"})
    c.post("/api/assistant/listen")
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert b"groceries do I need" not in f.read_bytes(), f


def test_lan_microphone_is_off_by_default_and_can_be_allowed(tmp_path, caplog):
    import logging
    phone, w = api(tmp_path, client_ip="192.168.1.131")
    assert phone.get("/api/assistant").json()["mic_allowed"] is False
    r = phone.post("/api/assistant/listen")
    assert r.status_code == 403 and "Settings → Assistant" in r.json()["detail"]
    assert "/record" not in w.calls
    # allow it in Settings → Assistant
    assert phone.patch("/api/settings/assistant", json={"mic_from_lan": True}).status_code == 200
    assert phone.get("/api/assistant").json()["mic_allowed"] is True
    with caplog.at_level(logging.INFO):
        r = phone.post("/api/assistant/listen")
    assert r.status_code == 200 and r.json()["source"] == "voice"
    assert w.calls[-2:] == ["/record", "/transcribe/c1"]
    assert "microphone started from 192.168.1.131" in caplog.text            # audit trail, no audio/text
    assert phone.post("/api/assistant/stop").status_code == 204
    # and switched off again: refused at once
    phone.patch("/api/settings/assistant", json={"mic_from_lan": False})
    assert phone.post("/api/assistant/listen").status_code == 403


def test_entrance_screen_always_allowed(tmp_path):
    tv, w = api(tmp_path)                                  # 127.0.0.1 = the Pi's own browser
    assert tv.get("/api/settings").json()["values"]["assistant"]["mic_from_lan"] is False
    assert tv.post("/api/assistant/listen").status_code == 200
