"""Entrance dashboard in a real browser: clock, city, weather scenes, advice, commute."""

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fixtures import SCENE_CODES, commute_payload, unavailable_weather, weather_payload

TZ = ZoneInfo("Asia/Kolkata")
LOCATION = {"location": {
    "city": "Pune", "region": "Maharashtra", "country": "India", "country_code": "IN",
    "latitude": 18.52, "longitude": 73.86, "timezone": "Asia/Kolkata", "source": "manual",
    "confirmed": True, "provider": "Open-Meteo geocoding", "updated_at": "2026-10-18T00:00:00Z",
}}


@pytest.fixture
def dash(chrome, server):
    (server.data_dir / "location.json").write_text(json.dumps(LOCATION))
    server.restart()
    page = chrome.new_page()
    page.viewport(1920, 1080)
    yield page, server
    assert page.errors == [], page.errors


def open_with(page, server, weather, commute=None):
    page.fixture("/api/weather", weather)
    page.fixture("/api/commute", commute or commute_payload("active"))
    page.goto(f"{server.url}/")
    page.wait_for("document.getElementById('weather').dataset.state !== 'loading'")


def test_clock_date_city_weather_advice(dash):
    page, server = dash
    open_with(page, server, weather_payload(63, 12))
    page.wait_for("document.getElementById('city').textContent === 'Pune'")
    clock = page.text("#clock-hm")
    now = datetime.now(TZ)
    assert re.fullmatch(r"\d{1,2}:\d{2}", clock), clock
    assert clock in {(now - timedelta(minutes=d)).strftime("%-I:%M") for d in (0, 1)}, clock
    assert page.text("#clock-ampm") == now.strftime("%p")
    assert now.strftime("%A") in page.text("#date")
    assert "Set manually" in page.text("#location-badge")
    assert page.text("#weather-temp").endswith("°")
    assert page.text("#weather-desc") == "Rain"
    assert page.js("document.querySelectorAll('#advice-list .advice__item').length") >= 1
    assert page.js("document.querySelectorAll('#hourly li').length") >= 1
    assert page.js("document.querySelectorAll('#daily li').length") == 5


def test_clock_ticks_without_network(dash):
    """The clock is local: it keeps advancing with the backend stopped."""
    page, server = dash
    open_with(page, server, weather_payload(0, 12))
    page.clear_fixtures()
    server.stop()
    before = page.text("#clock-hm")
    page.wait_for(f"document.getElementById('clock-hm').textContent !== {json.dumps(before)}", timeout=65)
    now = datetime.now(TZ)
    assert page.text("#clock-hm") in {(now - timedelta(minutes=d)).strftime("%-I:%M") for d in (0, 1)}
    server.start()
    # Connection-refused errors while the server was down are expected here.
    page.errors[:] = [e for e in page.errors if "ERR_CONNECTION_REFUSED" not in e]


@pytest.mark.parametrize("scene", list(SCENE_CODES))
def test_weather_scene_for_each_condition(dash, artifacts, scene):
    page, server = dash
    code, hour = SCENE_CODES[scene]
    open_with(page, server, weather_payload(code, hour))
    page.wait_for(f"document.getElementById('wx-scene').dataset.scene === {json.dumps(scene)}")
    assert page.js("document.getElementById('wx-scene').dataset.motion") == "live"
    # The scene stays inside the weather card, behind the content.
    inside = page.js("""(() => {
        const card = document.getElementById('weather').getBoundingClientRect();
        const sc = document.getElementById('wx-scene').getBoundingClientRect();
        const z = getComputedStyle(document.querySelector('.weather__content')).zIndex;
        return sc.left >= card.left - 1 && sc.right <= card.right + 1 && sc.top >= card.top - 1
            && sc.bottom <= card.bottom + 1 && getComputedStyle(document.getElementById('weather')).overflow === 'hidden'
            && Number(z) > Number(getComputedStyle(document.getElementById('wx-scene')).zIndex);
    })()""")
    assert inside
    # Text stays readable: temperature is opaque white-ish.
    assert page.js("getComputedStyle(document.getElementById('weather-temp')).color").startswith("rgb(244, 246, 251")
    counts = page.js("""(() => { const s = document.getElementById('wx-scene'); return {
        drops: s.querySelectorAll('.wxs-drop').length, flakes: s.querySelectorAll('.wxs-flake').length,
        stars: s.querySelectorAll('.wxs-star').length, clouds: s.querySelectorAll('.wxs-cloud').length,
        flash: s.querySelectorAll('.wxs-flash').length, sun: s.querySelectorAll('.wxs-sun').length,
        mist: s.querySelectorAll('.wxs-mist').length,
        animations: document.getAnimations().filter(a => s.contains(a.effect && a.effect.target)).length }; })()""")
    expect = {
        "clear-day": {"sun": 1}, "clear-night": {"stars": 22}, "partly-cloudy-day": {"sun": 1, "clouds": 3},
        "partly-cloudy-night": {"stars": 10, "clouds": 3}, "overcast": {"clouds": 5}, "fog": {"mist": 4},
        "drizzle": {"drops": 14}, "rain": {"drops": 30}, "heavy-rain": {"drops": 54},
        "thunderstorm": {"drops": 44, "flash": 1}, "snow": {"flakes": 26}, "unknown": {},
    }[scene]
    for k, v in expect.items():
        assert counts[k] == v, (scene, counts)
    if scene != "unknown":
        assert counts["animations"] > 0, counts
    # Lightweight: never more than ~60 animated nodes.
    assert sum(counts[k] for k in ("drops", "flakes", "stars", "clouds", "mist")) <= 60
    page.screenshot(str(artifacts / f"scene-{scene}.png"))


def test_stale_weather_is_static_and_labelled(dash):
    page, server = dash
    open_with(page, server, weather_payload(63, 12, status="stale"))
    page.wait_for("document.getElementById('wx-scene').dataset.motion === 'static'")
    assert "Weather service unavailable" in page.text("#weather-meta")
    # Looping CSS animations must be paused (a one-off opacity transition is fine).
    running = page.js("""document.getAnimations().filter(a => a instanceof CSSAnimation
                          && document.getElementById('wx-scene').contains(a.effect && a.effect.target)
                          && a.playState === 'running').length""")
    assert running == 0


def test_sample_weather_is_static_and_labelled(dash):
    page, server = dash
    open_with(page, server, weather_payload(63, 12, is_mock=True))
    page.wait_for("document.getElementById('wx-scene').dataset.motion === 'static'")
    assert "Sample data" in page.text("#weather-meta")


def test_unavailable_weather_has_no_animation(dash):
    page, server = dash
    open_with(page, server, unavailable_weather())
    page.wait_for("document.getElementById('weather').dataset.state === 'unavailable'")
    assert page.js("document.getElementById('wx-scene').dataset.motion") == "none"
    assert page.js("document.getElementById('wx-scene').children.length") == 0
    assert "Set your home location" in page.text("#weather-message-title")


def test_reduced_motion(dash, artifacts):
    page, server = dash
    page.reduced_motion(True)
    open_with(page, server, weather_payload(65, 12))
    page.wait_for("document.getElementById('wx-scene').dataset.motion === 'reduced'")
    running = page.js("""document.getAnimations().filter(a => a instanceof CSSAnimation
                          && a.playState === 'running').length""")
    assert running == 0
    assert page.js("getComputedStyle(document.querySelector('.wxs-drop')).display") == "none"
    page.screenshot(str(artifacts / "scene-reduced-motion.png"))
    page.reduced_motion(False)


def test_commute_active_card(dash, artifacts):
    page, server = dash
    open_with(page, server, weather_payload(2, 9), commute_payload("active"))
    page.wait_for("document.getElementById('commute').dataset.state === 'active'")
    text = page.text("#commute")
    assert "Best time to leave" in text and "9:15 AM" in text
    assert "24 min" in text and "arrive 9:39 AM" in text
    assert "Forecast · TomTom historical traffic patterns" in text
    assert "Leaving now" in text and "live traffic" in text
    assert "Home → Office" in text
    assert "TomTom · updated" in text
    assert page.js("document.querySelectorAll('#commute .slot').length") == 17
    assert page.js("document.querySelectorAll('#commute .slot[data-best]').length") == 1
    assert page.js("document.querySelectorAll('#commute .slot[data-past]').length") == 4   # 7:00-7:45
    page.screenshot(str(artifacts / "dashboard-1920-commute-active.png"))


@pytest.mark.parametrize("kind, expected, absent", [
    ("unconfigured", "TomTom API key", "Best time"),
    ("after_window", "Earlier today", "Best time"),
    ("off_day", "No commute today", "Best time"),
])
def test_commute_states(dash, kind, expected, absent):
    page, server = dash
    open_with(page, server, weather_payload(0, 12), commute_payload(kind))
    page.wait_for(f"document.getElementById('commute').dataset.state === {json.dumps(kind)}")
    text = page.text("#commute")
    assert expected in text and absent not in text
    assert "min" not in text or kind == "after_window"   # no invented durations


def test_real_commute_endpoint_without_key(chrome, make_server):
    srv = make_server(AURA_COMMUTE_ENABLED="true", AURA_COMMUTE_HOME="13.0087307,77.7381464",
                      AURA_COMMUTE_OFFICE="12.9927608,77.6995722", AURA_TOMTOM_API_KEY="")
    page = chrome.new_page()
    page.viewport(1440, 900)
    page.goto(f"{srv.url}/")
    page.wait_for("document.getElementById('commute').dataset.state === 'unconfigured'")
    assert "TomTom API key" in page.text("#commute")
    html = page.js("document.documentElement.outerHTML")
    assert "13.0087" not in html and "77.7381" not in html
    assert page.errors == []
