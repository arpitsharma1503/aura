import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.open_meteo import OpenMeteoProvider, parse_forecast
from app.weather import (
    Condition,
    MockWeatherProvider,
    WeatherProviderError,
    WeatherService,
)
from conftest import FakeNetwork, make_client, save_location
import app.open_meteo as open_meteo_mod


@pytest.fixture(autouse=True)
def _no_wait_before_aq_retry(monkeypatch):
    monkeypatch.setattr(open_meteo_mod, "AQ_RETRY_DELAY", 0)

TZ = "Asia/Kolkata"
HOME = SimpleNamespace(city="Pune", latitude=18.52, longitude=73.86, timezone=TZ)


def forecast_json(start: datetime | None = None, hours: int = 30, code: int = 61) -> dict:
    """A realistic Open-Meteo forecast response, starting at the current hour."""
    tz = ZoneInfo(TZ)
    start = (start or datetime.now(tz)).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(hours)]
    days = [(start.date() + timedelta(days=i)).isoformat() for i in range(7)]
    return {
        "latitude": 18.5, "longitude": 73.875, "utc_offset_seconds": 19800, "timezone": TZ,
        "current": {
            "time": start.strftime("%Y-%m-%dT%H:%M"), "interval": 900, "temperature_2m": 24.1,
            "apparent_temperature": 26.3, "relative_humidity_2m": 88, "is_day": 1,
            "precipitation": 0.4, "weather_code": code, "wind_speed_10m": 9.4,
            "wind_gusts_10m": 21.2, "uv_index": 3.15,
        },
        "hourly": {
            "time": times,
            "temperature_2m": [24.0] * hours,
            "apparent_temperature": [26.0] * hours,
            "precipitation_probability": [70] * hours,
            "precipitation": [0.6] * hours,
            "weather_code": [code] * hours,
            "wind_speed_10m": [9.0] * hours,
            "wind_gusts_10m": [20.0] * hours,
            "uv_index": [3.0] * hours,
            "is_day": [1] * hours,
        },
        "daily": {
            "time": days,
            "weather_code": [61, 95, 3, 0, 2, 80, 45],
            "temperature_2m_max": [28.0] * 7, "temperature_2m_min": [21.0] * 7,
            "sunrise": [f"{d}T06:21" for d in days], "sunset": [f"{d}T18:04" for d in days],
            "precipitation_probability_max": [90] * 7, "precipitation_sum": [12.3] * 7,
            "uv_index_max": [6.5] * 7,
        },
    }


def aq_json(start: datetime | None = None, aqi: int = 57) -> dict:
    tz = ZoneInfo(TZ)
    start = (start or datetime.now(tz)).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(30)]
    return {"timezone": TZ, "current": {"time": times[0], "us_aqi": aqi, "pm2_5": 21.7},
            "hourly": {"time": times, "us_aqi": [aqi] * 30}}


def open_meteo_network(forecast=None, aq=None) -> FakeNetwork:
    return FakeNetwork({
        "api.open-meteo.com": forecast if forecast is not None else (200, forecast_json()),
        "air-quality-api.open-meteo.com": aq if aq is not None else (200, aq_json()),
    })


def run(coro):
    return asyncio.run(coro)


class CountingProvider:
    """Wraps a provider; can be switched to fail."""

    name = "test"
    is_mock = False

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.no_aq = False

    async def fetch(self, location, timezone):
        self.calls += 1
        if self.fail:
            raise WeatherProviderError("Weather service unavailable")
        return parse_forecast(forecast_json(), location.city, None if self.no_aq else aq_json())


class Clock:
    def __init__(self) -> None:
        self.t = datetime.now().timestamp()

    def __call__(self) -> float:
        return self.t


# --- parsing -----------------------------------------------------------------

def test_parse_open_meteo_forecast():
    snap = parse_forecast(forecast_json(), "Pune", aq_json(aqi=160))
    assert snap.is_mock is False
    assert snap.source == "Open-Meteo"
    assert "Open-Meteo" in snap.attribution
    assert snap.timezone == TZ
    c = snap.current
    assert (c.temperature_c, c.feels_like_c, c.humidity_pct) == (24.1, 26.3, 88)
    assert c.condition == Condition.RAIN and c.description == "Light rain"
    assert c.uv_index == 3.15 and c.wind_gust_kph == 21.2
    assert c.precipitation_chance_pct == 70
    assert len(snap.hourly) == 30
    assert snap.hourly[0].time.tzinfo is not None
    assert snap.hourly[0].us_aqi == 160
    assert len(snap.daily) == 7
    assert snap.daily[1].condition == Condition.THUNDERSTORM
    assert snap.sunrise.hour == 6 and snap.sunset.hour == 18
    assert snap.air_quality.us_aqi == 160 and snap.air_quality.category == "Unhealthy"


def test_parse_rejects_incomplete_response():
    data = forecast_json()
    del data["current"]
    with pytest.raises(WeatherProviderError):
        parse_forecast(data, "Pune")


def test_unknown_weather_code_does_not_crash():
    snap = parse_forecast(forecast_json(code=42), "Pune")
    assert snap.current.description == "Unknown"


# --- provider over HTTP --------------------------------------------------------

def provider_for(net: FakeNetwork) -> OpenMeteoProvider:
    return OpenMeteoProvider(httpx.AsyncClient(transport=net.transport))


def test_provider_sends_rounded_coordinates():
    net = open_meteo_network()
    precise = SimpleNamespace(city="Pune", latitude=18.519571, longitude=73.855357, timezone=TZ)
    run(provider_for(net).fetch(precise, TZ))
    forecast_req = next(r for r in net.calls if r.url.host == "api.open-meteo.com")
    assert forecast_req.url.params["latitude"] == "18.52"
    assert forecast_req.url.params["longitude"] == "73.86"
    assert forecast_req.url.params["timezone"] == "auto"


def test_provider_timeout_is_reported():
    net = open_meteo_network(forecast=httpx.ReadTimeout("slow"))
    with pytest.raises(WeatherProviderError, match="timed out"):
        run(provider_for(net).fetch(HOME, TZ))


def test_provider_server_error_is_reported():
    net = open_meteo_network(forecast=(503, {"error": True, "reason": "busy"}))
    with pytest.raises(WeatherProviderError):
        run(provider_for(net).fetch(HOME, TZ))


def test_provider_rate_limit_is_reported():
    net = open_meteo_network(forecast=(429, {"error": True, "reason": "limit"}))
    with pytest.raises(WeatherProviderError, match="rate limit"):
        run(provider_for(net).fetch(HOME, TZ))


def test_air_quality_failure_is_optional():
    net = open_meteo_network(aq=httpx.ConnectError("down"))
    snap = run(provider_for(net).fetch(HOME, TZ))
    assert snap.air_quality is None
    assert snap.current.temperature_c == 24.1


# --- caching service -------------------------------------------------------

def service(tmp_path, provider, clock, **kw) -> WeatherService:
    return WeatherService(provider, tmp_path / "weather_cache.json", clock=clock,
                          refresh_seconds=900, retry_seconds=120, max_stale_hours=6, **kw)


def test_results_are_cached_within_refresh_interval(tmp_path):
    p, clock = CountingProvider(), Clock()
    svc = service(tmp_path, p, clock)
    assert run(svc.get(HOME, TZ)).status == "ok"
    clock.t += 600
    assert run(svc.get(HOME, TZ)).status == "ok"
    assert p.calls == 1
    clock.t += 400
    run(svc.get(HOME, TZ))
    assert p.calls == 2


def test_failure_without_cache_is_error(tmp_path):
    p = CountingProvider()
    p.fail = True
    res = run(service(tmp_path, p, Clock()).get(HOME, TZ))
    assert res.status == "error"
    assert res.data is None
    assert "unavailable" in res.message


def test_failure_serves_last_good_data_as_stale(tmp_path):
    p, clock = CountingProvider(), Clock()
    svc = service(tmp_path, p, clock)
    first = run(svc.get(HOME, TZ))
    p.fail = True
    clock.t += 1000
    res = run(svc.get(HOME, TZ))
    assert res.status == "stale"
    assert res.data is not None
    assert res.data.fetched_at == first.data.fetched_at   # honest "last updated" time
    assert res.message


def test_retry_backoff_after_failure(tmp_path):
    p, clock = CountingProvider(), Clock()
    p.fail = True
    svc = service(tmp_path, p, clock)
    run(svc.get(HOME, TZ))
    clock.t += 60
    run(svc.get(HOME, TZ))
    assert p.calls == 1          # still backing off
    clock.t += 61
    p.fail = False
    assert run(svc.get(HOME, TZ)).status == "ok"
    assert p.calls == 2


def test_very_old_cache_is_not_shown(tmp_path):
    p, clock = CountingProvider(), Clock()
    svc = service(tmp_path, p, clock)
    run(svc.get(HOME, TZ))
    p.fail = True
    clock.t += 7 * 3600
    assert run(svc.get(HOME, TZ)).status == "error"


def test_cache_survives_restart(tmp_path):
    p, clock = CountingProvider(), Clock()
    run(service(tmp_path, p, clock).get(HOME, TZ))
    assert (tmp_path / "weather_cache.json").exists()
    p2 = CountingProvider()
    p2.fail = True
    clock.t += 1000
    res = run(service(tmp_path, p2, clock).get(HOME, TZ))
    assert res.status == "stale" and res.data.city == "Pune"


def test_location_change_triggers_fresh_fetch(tmp_path):
    p, clock = CountingProvider(), Clock()
    svc = service(tmp_path, p, clock)
    run(svc.get(HOME, TZ))
    elsewhere = SimpleNamespace(city="Delhi", latitude=28.67, longitude=77.22, timezone=TZ)
    res = run(svc.get(elsewhere, TZ))
    assert p.calls == 2 and res.data.city == "Delhi"


def test_stale_cache_for_other_location_is_never_used(tmp_path):
    p, clock = CountingProvider(), Clock()
    svc = service(tmp_path, p, clock)
    run(svc.get(HOME, TZ))
    p.fail = True
    elsewhere = SimpleNamespace(city="Delhi", latitude=28.67, longitude=77.22, timezone=TZ)
    assert run(svc.get(elsewhere, TZ)).status == "error"


def test_mock_data_is_never_persisted(tmp_path):
    svc = service(tmp_path, MockWeatherProvider(), Clock())
    assert run(svc.get(HOME, TZ)).is_mock is True
    assert not (tmp_path / "weather_cache.json").exists()


# --- end to end through the API ----------------------------------------------

def test_api_live_weather_then_outage(tmp_path):
    save_location(tmp_path)
    net = open_meteo_network()
    client = make_client(tmp_path, network=net, weather_provider="open_meteo",
                         weather_refresh_seconds=60, weather_retry_seconds=10)
    body = client.get("/api/weather").json()
    assert body["status"] == "ok" and body["is_mock"] is False
    assert body["data"]["daily"] and body["data"]["sunrise"]
    assert body["recommendations"]
    assert "latitude" not in str(body["data"])  # snapshot carries no coordinates

    # Provider goes down and the cache expires: last data is kept, marked stale.
    net.routes["api.open-meteo.com"] = httpx.ConnectError("down")
    client.app.state.weather._entry.fetched_ts -= 120
    stale = client.get("/api/weather").json()
    assert stale["status"] == "stale"
    assert stale["data"]["fetched_at"] == body["data"]["fetched_at"]


def test_empty_current_aqi_falls_back_to_this_hours_value():
    """Just after the hour Open-Meteo can send "current": {"us_aqi": null} while the hourly
    list has the value (seen live on 3 Nov): the dashboard used "Not available" for 15 min."""
    aq = aq_json(aqi=190)
    aq["current"]["us_aqi"] = None
    aq["current"]["pm2_5"] = None
    snap = parse_forecast(forecast_json(), "Greater Noida", aq)
    assert snap.air_quality is not None and snap.air_quality.us_aqi == 190 and snap.air_quality.category == "Unhealthy"
    assert snap.air_quality.pm2_5 is None                                  # unknown stays unknown
    empty = aq_json(aqi=190)
    empty["current"]["us_aqi"] = None
    empty["hourly"]["us_aqi"] = [None] * 30
    assert parse_forecast(forecast_json(), "Greater Noida", empty).air_quality is None   # nothing invented


def test_a_missed_air_quality_reading_keeps_the_last_one_for_three_hours(tmp_path):
    """Seen live: one refresh came back without air quality and the AQI vanished from the
    dashboard for 15 minutes (or longer after a restart, from the saved cache). A recent
    reading stays; an old one doesn't."""
    p, clock = CountingProvider(), Clock()
    svc = service(tmp_path, p, clock)
    assert run(svc.get(HOME, TZ)).data.air_quality.us_aqi == 57
    p.no_aq = True
    clock.t += 1000
    assert run(svc.get(HOME, TZ)).data.air_quality.us_aqi == 57          # kept (17 min old)
    clock.t += 10000
    assert run(svc.get(HOME, TZ)).data.air_quality is None               # over 3 h: not shown


def test_a_timed_out_air_quality_call_is_tried_once_more():
    """Seen live 15 Nov: ConnectTimeout on the air-quality call while the forecast worked."""
    tries = []
    def aq(request):
        tries.append(request)
        if len(tries) == 1:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(200, json=aq_json())
    snap = run(provider_for(open_meteo_network(aq=aq)).fetch(HOME, TZ))
    assert len(tries) == 2
    assert snap.air_quality is not None


def test_after_a_missed_air_quality_reading_it_asks_again_in_two_minutes(tmp_path):
    """Seen live: no refresh for over 2 h, then one miss, so the AQI went blank for the next 15 min."""
    p, clock = CountingProvider(), Clock()
    svc = service(tmp_path, p, clock)
    run(svc.get(HOME, TZ))
    p.no_aq = True
    clock.t += 4 * 3600                                                   # too old to keep
    assert run(svc.get(HOME, TZ)).data.air_quality is None
    calls = p.calls
    clock.t += 60
    run(svc.get(HOME, TZ))
    assert p.calls == calls                                               # not before retry_seconds
    p.no_aq = False
    clock.t += 70
    assert run(svc.get(HOME, TZ)).data.air_quality.us_aqi == 57           # back after 2 min, not 15
    assert p.calls == calls + 1
    clock.t += 300
    run(svc.get(HOME, TZ))
    assert p.calls == calls + 1                                           # normal 15 min refresh again


def test_air_quality_that_never_comes_stops_the_quick_retries(tmp_path):
    p, clock = CountingProvider(), Clock()
    p.no_aq = True
    svc = service(tmp_path, p, clock)
    for _ in range(12):
        run(svc.get(HOME, TZ))
        clock.t += 130
    assert p.calls <= 8                                                   # 1 + 5 quick retries, then every 15 min


def test_a_saved_cache_without_air_quality_asks_again_soon_after_a_restart(tmp_path):
    p, clock = CountingProvider(), Clock()
    p.no_aq = True
    run(service(tmp_path, p, clock).get(HOME, TZ))
    path = tmp_path / "weather_cache.json"
    import json as _json
    raw = _json.loads(path.read_text()); raw.pop("aq_misses", None); path.write_text(_json.dumps(raw))   # older file format
    p.no_aq = False
    svc = service(tmp_path, p, clock)                                     # the restart
    clock.t += 130
    assert run(svc.get(HOME, TZ)).data.air_quality.us_aqi == 57
