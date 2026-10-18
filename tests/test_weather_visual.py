from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.open_meteo import parse_forecast
from app.weather import (
    Condition,
    MockWeatherProvider,
    WeatherResponse,
    WeatherService,
    is_daytime,
    scene_for,
    visual_for,
)
from conftest import save_location
from test_weather import aq_json, forecast_json, open_meteo_network

TZ = ZoneInfo("Asia/Kolkata")


def at(h, m=0):
    return datetime(2026, 10, 19, h, m, tzinfo=TZ)


SUNRISE, SUNSET = at(6, 10), at(17, 59)


# --- code -> scene ---------------------------------------------------------------

@pytest.mark.parametrize("code, day, scene", [
    (0, True, "clear-day"), (0, False, "clear-night"), (1, True, "clear-day"),
    (2, True, "partly-cloudy-day"), (2, False, "partly-cloudy-night"),
    (3, True, "overcast"), (3, False, "overcast"),
    (45, True, "fog"), (48, False, "fog"),
    (51, True, "drizzle"), (55, True, "drizzle"), (57, True, "drizzle"),
    (61, True, "rain"), (63, True, "rain"), (80, True, "rain"), (81, True, "rain"),
    (65, True, "heavy-rain"), (67, True, "heavy-rain"), (82, True, "heavy-rain"),
    (71, True, "snow"), (75, True, "snow"), (86, True, "snow"),
    (95, True, "thunderstorm"), (96, False, "thunderstorm"), (99, True, "thunderstorm"),
    (42, True, "unknown"), (None, True, "unknown"),
])
def test_weather_code_to_scene(code, day, scene):
    assert scene_for(code, None, day) == scene


def test_code_wins_over_condition():
    # Provider says heavy rain (65): don't downgrade to generic "rain".
    assert scene_for(65, Condition.RAIN, True) == "heavy-rain"


def test_condition_fallback_when_no_code():
    assert scene_for(None, Condition.RAIN, True) == "rain"
    assert scene_for(None, Condition.CLEAR, False) == "clear-night"


def test_scene_never_depends_on_temperature():
    hot = parse_forecast(forecast_json(code=3), "Pune")
    hot.current.temperature_c = 44
    cold = parse_forecast(forecast_json(code=3), "Pune")
    cold.current.temperature_c = -5
    r_hot = visual_for(WeatherResponse(status="ok", data=hot), at(12))
    r_cold = visual_for(WeatherResponse(status="ok", data=cold), at(12))
    assert r_hot.scene == r_cold.scene == "overcast"


# --- day / night -------------------------------------------------------------------

@pytest.mark.parametrize("now, expected", [
    (at(6, 9), False), (at(6, 10), True), (at(12), True), (at(17, 58), True), (at(17, 59), False), (at(23), False),
])
def test_day_night_from_sunrise_sunset(now, expected):
    assert is_daytime(now, SUNRISE, SUNSET, fallback=not expected) is expected


def test_day_night_uses_location_time_not_utc():
    utc_noon_ist = datetime(2026, 10, 19, 6, 30, tzinfo=ZoneInfo("UTC")).astimezone(TZ)  # 12:00 IST
    assert is_daytime(utc_noon_ist, SUNRISE, SUNSET, fallback=False) is True


def test_day_night_fallback_without_sun_times():
    assert is_daytime(at(12), None, None, fallback=False) is False


def test_visual_overrides_provider_is_day_with_sun_times():
    snap = parse_forecast(forecast_json(code=0), "Pune")
    snap.current.is_day = True
    snap.sunrise, snap.sunset = SUNRISE, SUNSET
    v = visual_for(WeatherResponse(status="ok", data=snap), at(21))
    assert v.scene == "clear-night" and v.is_day is False


# --- motion: live vs static -------------------------------------------------------------

def test_live_data_animates():
    snap = parse_forecast(forecast_json(code=61), "Pune")
    v = visual_for(WeatherResponse(status="ok", data=snap), at(12))
    assert v.motion == "live" and v.scene == "rain"


def test_stale_data_is_static():
    snap = parse_forecast(forecast_json(code=61), "Pune")
    v = visual_for(WeatherResponse(status="stale", data=snap), at(12))
    assert v.motion == "static" and v.reason == "stale"


def test_mock_data_is_static():
    import asyncio
    snap = asyncio.run(MockWeatherProvider().fetch(SimpleNamespace(city="X"), "Asia/Kolkata"))
    v = visual_for(WeatherResponse(status="ok", is_mock=True, data=snap))
    assert v.motion == "static" and v.reason == "sample"


@pytest.mark.parametrize("status", ["unavailable", "error"])
def test_no_data_has_no_animation(status):
    v = visual_for(WeatherResponse(status=status))
    assert v.motion == "none" and v.scene == "unknown"


# --- API ------------------------------------------------------------------------------------

def test_api_includes_visual(tmp_path):
    from conftest import make_client
    save_location(tmp_path)
    c = make_client(tmp_path, network=open_meteo_network(), weather_provider="open_meteo")
    body = c.get("/api/weather").json()
    assert body["visual"]["scene"] in {"rain"}           # fixture uses code 61
    assert body["visual"]["motion"] == "live"


def test_api_visual_when_unavailable(client):
    assert client.get("/api/weather").json()["visual"] == {
        "scene": "unknown", "is_day": True, "motion": "none", "reason": "unavailable"}


# --- regression: cached data after midnight -------------------------------------------------

def test_today_fields_follow_the_date_after_midnight(tmp_path):
    yesterday_6pm = datetime.now(TZ).replace(hour=18, minute=0, second=0, microsecond=0) - timedelta(days=1)

    class P:
        name, is_mock = "p", False

        async def fetch(self, loc, tz):
            data = forecast_json(start=yesterday_6pm)
            data["daily"]["temperature_2m_max"] = [30.0, 25.0, 26, 27, 28, 29, 30]
            data["daily"]["sunrise"][1] = data["daily"]["time"][1] + "T06:12"
            return parse_forecast(data, loc.city, aq_json(start=yesterday_6pm))

    import asyncio
    clock_t = [yesterday_6pm.timestamp()]
    svc = WeatherService(P(), None, clock=lambda: clock_t[0], refresh_seconds=10**6, max_stale_hours=48)
    home = SimpleNamespace(city="Pune", latitude=18.52, longitude=73.86, timezone="Asia/Kolkata")
    first = asyncio.run(svc.get(home, "Asia/Kolkata"))
    assert first.data.high_c == 30.0
    clock_t[0] += 8 * 3600                                       # 02:00 next day, same cached snapshot
    later = asyncio.run(svc.get(home, "Asia/Kolkata"))
    assert later.data.high_c == 25.0                              # today's forecast, not yesterday's
    assert later.data.sunrise.date() == later.data.daily[0].date
