"""Wake word: phrase check, candidate selection, shared-stream capture, and the AURA endpoint."""

import asyncio

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.wake import Capture, WakeListener, split_wake
from conftest import FakeNetwork, make_settings
from test_assistant import FakeWorker


def test_split_wake():
    assert split_wake("Hey Aura, what's the weather today?") == "what's the weather today?"
    assert split_wake("Hey, Aura.") == ""
    assert split_wake("Aora, summarize my day.") == "summarize my day."
    assert split_wake("Uh, hey Aura how is my commute") == "how is my commute"
    for not_wake in ["Hey, are you ready?", "Hey Laura, come here.", "Her aura was really calm today.",
                     "Oh, our car is outside.", "What's the weather today?", "I think the aura of this place is calm", ""]:
        assert split_wake(not_wake) is None, not_wake


def listener():
    async def t(pcm, vad, wake_check=False):
        return ""

    async def n(ev):
        pass
    return WakeListener("hw", __import__("pathlib").Path("/nonexistent"), t, n)


def test_candidate_needs_both_words_confident_and_adjacent():
    w = listener()
    good = {"result": [{"word": "hey", "conf": 0.95, "start": 1.0, "end": 1.2}, {"word": "aura", "conf": 0.9, "start": 1.25, "end": 1.6}]}
    assert abs(w._candidate(good) - 0.6) < 1e-9                         # from "hey" to the last word
    low = {"result": [{"word": "hey", "conf": 0.95, "start": 1.0, "end": 1.2}, {"word": "aura", "conf": 0.5, "start": 1.25, "end": 1.6}]}
    assert w._candidate(low) is None
    apart = {"result": [{"word": "hey", "conf": 1, "start": 1.0, "end": 1.2}, {"word": "aura", "conf": 1, "start": 2.5, "end": 2.8}]}
    assert w._candidate(apart) is None
    assert w._candidate({"text": ""}) is None


def test_capture_from_shared_stream_ends_after_pause():
    async def go():
        cap = Capture(10)
        quiet, loud = b"\x10\x00" * 1440, (b"\xff\x0f" + b"\x01\xf0") * 720
        for _ in range(5):
            cap.feed(quiet)
        for _ in range(12):
            cap.feed(loud)
        for _ in range(40):
            cap.feed(quiet)
        pcm, ep, reason = await asyncio.wait_for(cap.done, 1)
        return ep, reason
    ep, reason = asyncio.run(go())
    assert reason == "done" and ep.heard_speech


def test_status_reports_missing_model():
    w = listener()
    assert w.status() == {"enabled": False, "running": False, "phrase": "hey aura", "error": None, "model": False}


# ---------------- AURA endpoint ----------------

def app_client(tmp_path, ip="127.0.0.1", **settings):
    worker = FakeWorker()
    app = create_app(make_settings(tmp_path, edit_access="lan", **settings), transport=FakeNetwork().transport,
                     voice_transport=httpx.MockTransport(worker))
    return TestClient(app, base_url="http://localhost", client=(ip, 50000)), worker


def test_wake_events_ignored_unless_enabled(tmp_path):
    c, _ = app_client(tmp_path)
    assert c.post("/api/assistant/wake", json={"event": "question", "text": "what time is it"}).status_code == 204
    assert c.get("/api/assistant").json()["transcript"] == []          # off by default: nothing happens


def test_wake_question_is_answered(tmp_path):
    c, _ = app_client(tmp_path)
    c.patch("/api/settings/assistant", json={"wake_word": True})
    c.post("/api/assistant/wake", json={"event": "listening", "max_seconds": 10})
    assert c.app.state.voice.state == "listening"
    c.post("/api/assistant/wake", json={"event": "question", "text": "what groceries do I need"})
    t = c.get("/api/assistant").json()["transcript"][-1]
    assert t["source"] == "voice" and t["answer"]["intent"] == "groceries"
    assert c.app.state.voice.state == "answered"
    c.post("/api/assistant/wake", json={"event": "no_speech"})
    assert c.app.state.voice.state == "error" and "Hey Aura" in c.app.state.voice.error


def test_wake_endpoint_is_local_only(tmp_path):
    phone, _ = app_client(tmp_path, ip="192.168.1.131")
    phone.patch("/api/settings/assistant", json={"wake_word": True})
    assert phone.post("/api/assistant/wake", json={"event": "question", "text": "x"}).status_code == 403
    assert phone.post("/api/assistant/wake", content=b"[]", headers={"content-type": "application/json"}).status_code == 403


def test_setting_change_is_pushed_to_the_worker(tmp_path):
    c, worker = app_client(tmp_path)
    worker.calls.clear()
    c.patch("/api/settings/assistant", json={"wake_word": True})
    assert "/wake" in worker.calls


def test_misheard_wake_words_and_routing():
    assert split_wake("Biora.") == "" and split_wake("AYORA.") == ""
    assert split_wake("Hey Ora, what groceries do I need?") == "what groceries do I need?"
    from app.assistant import route
    assert route("Order the weather.").name == "weather"          # misheard "What's the weather"
    assert route("Delete all my groceries").name == "action"      # changes are still refused


def test_button_question_with_wake_phrase_is_stripped(tmp_path):
    from test_assistant import api, FakeWorker
    c, _ = api(tmp_path, FakeWorker(text="Hey Aura, what groceries do I need?"))
    turn = c.post("/api/assistant/listen").json()
    assert turn["question"] == "what groceries do I need?" and turn["answer"]["intent"] == "groceries"
    c2, _ = api(tmp_path, FakeWorker(text="Biora."))
    turn = c2.post("/api/assistant/listen").json()
    assert turn["question"] == "Hey Aura" and turn["answer"]["intent"] == "greeting"


def test_candidate_span_covers_the_question():
    w = listener()
    words = {"result": [{"word": "hey", "conf": 1, "start": 0.3, "end": 0.5}, {"word": "aura", "conf": 0.95, "start": 0.55, "end": 0.9},
                        {"word": "[unk]", "conf": 1, "start": 1.0, "end": 2.6}]}
    assert abs(w._candidate(words) - 2.3) < 1e-9


def test_last_utterance_finds_the_whole_phrase():
    import struct
    from app.wake import last_utterance
    noise = lambda sec: struct.pack("<h", 30) * int(16000 * sec)
    speech = lambda sec: struct.pack("<hh", 900, -900) * int(8000 * sec)
    # earlier chatter, a long pause, then "Hey Aura" + short pause + question, then quiet
    pcm = speech(1) + noise(2) + speech(0.6) + noise(0.4) + speech(1.5) + noise(1.2)
    out = last_utterance(pcm)
    assert 2.4 * 32000 <= len(out) <= 3.2 * 32000          # 0.6 + 0.4 + 1.5 s plus margins, not the chatter
    quiet = noise(3)
    assert last_utterance(quiet) == quiet
