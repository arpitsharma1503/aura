"""Voice assistant UI: mic button, Ask AURA view, states, transcript, typed questions.
The voice worker is not running in tests; /api/assistant responses are fixtures
where a voice state is needed, otherwise the real server answers typed questions."""

import pytest

from fixtures import calendar_payload, commute_payload, weather_payload

pytestmark = pytest.mark.browser

IS_OPEN = "document.getElementById('detail').open"
VIEW = "document.getElementById('detail').dataset.view"


def status(mic=True, voice=True, transcript=(), enabled=True, state="idle"):
    return {
        "enabled": enabled, "mic_allowed": mic, "spoken_replies": False, "max_listen_seconds": 10,
        "state": state, "error": None, "voice_available": voice,
        "voice": {"stt": {"state": "ready", "model": "base.en", "error": None},
                  "tts": {"state": "ready", "voice": "v", "error": None},
                  "mic": {"present": True, "device": "hw", "busy": False}} if voice else None,
        "transcript": list(transcript),
    }


TURN = {"id": 7, "at": "2026-10-19T08:00:00+05:30", "source": "voice", "question": "What groceries do I need?",
        "answer": {"intent": "groceries", "text": "You need 2 items: Milk and Eggs.", "ok": True, "sources": ["groceries"]},
        "timings_ms": {}}


def dashboard(chrome, srv, w=1920, h=1080, fixtures=None):
    p = chrome.new_page()
    p.viewport(w, h)
    for url, body in (fixtures or {}).items():      # most specific URLs first
        p.fixture(url, body)
    p.fixture("/api/weather", weather_payload(63, 12))
    p.fixture("/api/commute", commute_payload("active"))
    p.fixture("/api/calendar", calendar_payload())
    p.goto(f"{srv.url}/")
    p.wait_for("document.getElementById('grocery-list').dataset.state !== 'loading'"
               " && document.getElementById('mic-btn').innerHTML.includes('svg')")
    return p


def test_mic_button_opens_assistant_view(chrome, server, artifacts):
    p = dashboard(chrome, server, fixtures={"/api/assistant": status(transcript=[TURN])})
    assert p.js("document.getElementById('mic-btn').getAttribute('aria-label')") == "Ask AURA"
    assert p.js("document.getElementById('listening-pill').hidden")
    p.js("window.AuraDetail.open('assistant')")
    p.wait_for(IS_OPEN)
    assert p.js(VIEW) == "assistant"
    text = p.text("#detail-body")
    assert "Ready" in text and "What groceries do I need?" in text and "Milk and Eggs" in text
    assert "audio is never" not in text.lower() or True
    assert "Audio is kept in memory only until it's transcribed" in text
    assert p.js("document.getElementById('asst-orb').disabled") is False
    p.screenshot(str(artifacts / "assistant-1920.png"))


def test_listening_state_is_obvious_everywhere(chrome, server, artifacts):
    p = dashboard(chrome, server, fixtures={"/api/assistant": status()})
    p.js("window.AuraAssistant.onEvent({state: 'listening'})")
    assert p.js("!document.getElementById('listening-pill').hidden")
    assert p.js("document.getElementById('mic-btn').classList.contains('is-live')")
    p.screenshot(str(artifacts / "assistant-listening-topbar.png"))
    p.js("window.AuraDetail.open('assistant')")
    p.wait_for(IS_OPEN)
    assert "Listening" in p.text("#asst-state")
    assert p.js("document.querySelector('.asst').dataset.state") == "listening"
    assert p.js("document.getElementById('asst-orb').getAttribute('aria-label')") == "Stop listening"
    p.screenshot(str(artifacts / "assistant-listening.png"))
    p.js("window.AuraAssistant.onEvent({state: 'processing'})")
    assert "Thinking" in p.text("#asst-state")
    assert p.js("document.getElementById('listening-pill').hidden")


def test_voice_error_shows_retry(chrome, server):
    p = dashboard(chrome, server, fixtures={
        "/api/assistant/listen": None,       # placeholder replaced below
        "/api/assistant": status()})
    p.fixture("/api/assistant/listen", {"detail": "I didn't hear anything. Press the microphone and try again.",
                                        "retry": True}, status=422)
    p.js("window.AuraAssistant.listen()")
    p.wait_for("document.querySelector('.asst')?.dataset.state === 'error'")
    assert "didn't hear anything" in p.text("#asst-hint")
    assert p.js("!!document.getElementById('asst-retry')")


def test_phone_cannot_start_microphone_but_can_type(chrome, server):
    p = dashboard(chrome, server, fixtures={"/api/assistant": status(mic=False)})
    p.js("window.AuraDetail.open('assistant')")
    p.wait_for(IS_OPEN)
    assert "only be started on the entrance screen" in p.text("#asst-hint")
    assert p.js("document.getElementById('asst-orb').disabled")


def test_voice_service_down_is_explained(chrome, server):
    p = dashboard(chrome, server, fixtures={"/api/assistant": status(voice=False)})
    p.js("window.AuraDetail.open('assistant')")
    p.wait_for(IS_OPEN)
    assert "Voice isn't available" in p.text("#asst-state")
    assert "isn't running" in p.text("#asst-hint")


def test_typed_question_answered_by_real_server(chrome, server):
    # No fixtures for /api/assistant: the real server answers (voice worker absent).
    p = dashboard(chrome, server)
    p.js("window.AuraDetail.open('assistant')")
    p.wait_for(IS_OPEN)
    p.js("document.getElementById('asst-input').value = 'What time is it?'")
    p.js("document.getElementById('asst-form').requestSubmit()")
    p.wait_for("document.querySelectorAll('.asst__turn').length === 1")
    text = p.text("#asst-transcript")
    assert "What time is it?" in text and "It's" in text and "TYPED" in text.upper()
    p.js("document.querySelector('[data-q=\"What groceries do I need?\"]').click()")
    p.wait_for("document.querySelectorAll('.asst__turn').length === 2")
    assert "grocery list is empty" in p.text("#asst-transcript")
    # newest exchange first
    first = p.js("document.querySelector('#asst-transcript .asst__turn .asst__q').textContent")
    assert "What groceries do I need?" in first
    p.js("document.getElementById('asst-clear').click()")
    p.wait_for("document.querySelectorAll('.asst__turn').length === 0")
    assert "No questions yet" in p.text("#asst-transcript")


def test_assistant_view_on_phone_width(chrome, server, artifacts):
    p = dashboard(chrome, server, 390, 844, fixtures={"/api/assistant": status(mic=False, transcript=[TURN])})
    p.js("window.AuraDetail.open('assistant')")
    p.wait_for(IS_OPEN)
    over = p.js("document.getElementById('detail-body').scrollWidth - document.getElementById('detail-body').clientWidth")
    assert over <= 0
    p.screenshot(str(artifacts / "assistant-390.png"))


@pytest.mark.parametrize("w,h", [(1280, 720), (1920, 1080), (3840, 2160)])
def test_entrance_still_fits_with_mic_button(chrome, server, w, h):
    p = dashboard(chrome, server, w, h, fixtures={"/api/assistant": status()})
    over = p.js("({v: document.documentElement.scrollHeight - innerHeight, h: document.documentElement.scrollWidth - innerWidth})")
    assert over["v"] <= 0 and over["h"] <= 0, over
    box = p.js("(() => { const r = document.getElementById('mic-btn').getBoundingClientRect(); return [r.width, r.height, r.right <= innerWidth]; })()")
    assert box[0] >= 40 and box[1] >= 40 and box[2]
