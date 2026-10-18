from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.recommendations import build_recommendations
from app.weather import AirQuality, Condition, CurrentWeather, HourlyForecast, WeatherSnapshot

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 18, 9, 10, tzinfo=TZ)   # 9:10 AM


def hour(i: int, *, temp=22.0, feels=None, cond=Condition.CLEAR, rain=0, mm=0.0,
         wind=8.0, gust=15.0, uv=None, aqi=None, is_day=True) -> HourlyForecast:
    t = NOW.replace(minute=0) + timedelta(hours=i)
    return HourlyForecast(
        time=t, temperature_c=temp, feels_like_c=temp if feels is None else feels,
        condition=cond, precipitation_chance_pct=rain, precipitation_mm=mm,
        wind_kph=wind, wind_gust_kph=gust, uv_index=3.0 if uv is None else uv,
        us_aqi=aqi, is_day=is_day,
    )


def snap(hours=None, *, cond=Condition.CLEAR, humidity=50, mm_now=0.0, aqi=None) -> WeatherSnapshot:
    hours = hours if hours is not None else [hour(i, uv=2) for i in range(8)]
    h0 = hours[0] if hours else hour(0)
    return WeatherSnapshot(
        city="Test", timezone="Asia/Kolkata", observed_at=NOW, fetched_at=NOW,
        source="test", is_mock=False,
        current=CurrentWeather(
            temperature_c=h0.temperature_c, feels_like_c=h0.feels_like_c, condition=cond,
            description="x", humidity_pct=humidity, wind_kph=h0.wind_kph or 0,
            wind_gust_kph=h0.wind_gust_kph, uv_index=h0.uv_index, precipitation_mm=mm_now,
            precipitation_chance_pct=h0.precipitation_chance_pct, is_day=True,
        ),
        high_c=25, low_c=18, hourly=hours,
        air_quality=AirQuality(us_aqi=aqi, category="x") if aqi is not None else None,
    )


def recs(s, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("limit", 10)
    return build_recommendations(s, **kw)


def ids(s, **kw):
    return [r.id for r in recs(s, **kw)]


def by_id(s, rid, **kw):
    return next(r for r in recs(s, **kw) if r.id == rid)


# --- general ------------------------------------------------------------

def test_pleasant_day():
    out = ids(snap())
    assert "pleasant" in out
    assert "bike" in out


def test_every_recommendation_has_an_explanation():
    s = snap([hour(i, feels=34, rain=80, mm=2, uv=9) for i in range(8)], humidity=80)
    for r in recs(s):
        assert len(r.detail) > 10


# --- rain over the travel window ----------------------------------------

def test_rain_later_in_window_means_umbrella_with_timing():
    hours = [hour(i, uv=2) for i in range(3)] + [hour(3, rain=70, mm=0.8, cond=Condition.RAIN, uv=2)] + \
            [hour(i, uv=2) for i in range(4, 8)]
    r = by_id(snap(hours), "umbrella")
    assert r.priority == "high"
    assert "12 PM" in r.detail and "70%" in r.detail


def test_rain_beyond_window_is_ignored():
    hours = [hour(i, uv=2) for i in range(7)] + [hour(7, rain=90, mm=5, cond=Condition.RAIN)]
    assert "umbrella" not in ids(snap(hours), window_hours=6)
    assert "raincoat" not in ids(snap(hours), window_hours=6)
    assert "umbrella" in ids(snap(hours), window_hours=8) or "raincoat" in ids(snap(hours), window_hours=8)


def test_raining_now():
    r = by_id(snap(cond=Condition.RAIN, mm_now=0.5), "umbrella")
    assert "raining now" in r.detail


def test_heavy_rain_means_raincoat():
    hours = [hour(i, rain=90, mm=2.0, cond=Condition.RAIN, uv=1) for i in range(6)]
    out = ids(snap(hours))
    assert "raincoat" in out and "umbrella" not in out
    assert "heavy rain" in by_id(snap(hours), "raincoat").detail


def test_wind_with_rain_means_raincoat():
    hours = [hour(i, rain=70, mm=0.3, gust=45, uv=1) for i in range(6)]
    assert "too windy" in by_id(snap(hours), "raincoat").detail


def test_possible_rain_is_low_priority():
    hours = [hour(i, rain=40, uv=2) for i in range(6)]
    r = by_id(snap(hours), "umbrella-maybe")
    assert r.priority == "low"


def test_low_rain_chance_no_umbrella():
    hours = [hour(i, rain=20, uv=2) for i in range(6)]
    assert not {"umbrella", "umbrella-maybe", "raincoat"} & set(ids(snap(hours)))


# --- temperature & clothing ---------------------------------------------

@pytest.mark.parametrize("feels, expected", [
    (34, "light-clothes"), (22, "pleasant"), (12, "jacket"), (3, "coat"),
])
def test_temperature_bands(feels, expected):
    hours = [hour(i, temp=feels, feels=feels, uv=2) for i in range(6)]
    assert expected in ids(snap(hours))


def test_cold_later_today_means_coat():
    hours = [hour(i, feels=18, uv=2) for i in range(4)] + [hour(4, feels=6, uv=0), hour(5, feels=5, uv=0)]
    r = by_id(snap(hours), "coat")
    assert "5°" in r.detail and "2 PM" in r.detail


def test_big_swing_means_layers():
    hours = [hour(i, feels=12 + i * 2.5, uv=2) for i in range(6)]
    assert "layers" in ids(snap(hours))


def test_heat_means_water_bottle():
    hours = [hour(i, feels=36, uv=2) for i in range(6)]
    r = by_id(snap(hours), "water")
    assert r.priority == "high"
    assert "36°" in r.detail


def test_warm_and_humid_means_water():
    hours = [hour(i, feels=28, uv=2) for i in range(6)]
    assert by_id(snap(hours, humidity=85), "water").priority == "low"


# --- UV -----------------------------------------------------------------

def test_uv_peak_in_window():
    hours = [hour(i, uv=u) for i, u in enumerate([3, 5, 7, 9, 8, 6])]
    r = by_id(snap(hours), "uv")
    assert r.priority == "high"
    assert "Sunscreen and sunglasses" == r.title
    assert "12 PM" in r.detail


def test_uv_ignored_at_night():
    hours = [hour(i, uv=9, is_day=False) for i in range(6)]
    assert "uv" not in ids(snap(hours))


def test_moderate_uv_suggests_sunglasses():
    hours = [hour(i, uv=4) for i in range(6)]
    assert "sunglasses" in ids(snap(hours))


# --- severe & air quality ---------------------------------------------------

def test_thunderstorm_first():
    hours = [hour(0, uv=2), hour(1, cond=Condition.THUNDERSTORM, rain=90, mm=4, uv=2)] + \
            [hour(i, uv=2) for i in range(2, 6)]
    out = recs(snap(hours))
    assert out[0].id == "storm"
    assert "10 AM" in out[0].detail


def test_severe_gusts():
    hours = [hour(i, gust=70, uv=2) for i in range(6)]
    assert by_id(snap(hours), "wind").priority == "high"


def test_unhealthy_air():
    hours = [hour(i, aqi=170, uv=2) for i in range(6)]
    r = by_id(snap(hours), "air")
    assert r.priority == "high" and "170" in r.detail


def test_current_aqi_used_when_hourly_missing():
    assert by_id(snap(aqi=120), "air").priority == "medium"


# --- bike vs car ------------------------------------------------------------

def test_bike_in_good_conditions_mentions_no_traffic():
    r = by_id(snap(), "bike")
    assert "traffic isn't checked" in r.detail


@pytest.mark.parametrize("kw, reason", [
    ({"rain": 60}, "rain"),
    ({"gust": 45}, "gusts"),
    ({"feels": 36}, "heat"),
    ({"feels": 2}, "cold"),
    ({"aqi": 160}, "air quality"),
])
def test_car_when_conditions_poor(kw, reason):
    hours = [hour(i, uv=2, **kw) for i in range(6)]
    r = by_id(snap(hours), "car")
    assert reason in r.detail
    assert "traffic isn't checked" in r.detail


def test_no_traffic_claims_anywhere():
    for s in (snap(), snap([hour(i, rain=80, mm=3) for i in range(6)])):
        for r in recs(s):
            text = (r.title + r.detail).lower()
            assert "traffic" not in text or "traffic isn't checked" in text


def test_no_bike_advice_without_enough_data():
    hours = [hour(i, uv=2).model_copy(update={"precipitation_chance_pct": None}) for i in range(6)]
    assert not {"bike", "car"} & set(ids(snap(hours)))


# --- mechanics ------------------------------------------------------------

def test_falls_back_to_current_when_no_hourly():
    s = snap([])
    s.current.condition = Condition.RAIN
    s.current.precipitation_mm = 1.0
    assert "umbrella" in ids(s)


def test_24h_time_format():
    hours = [hour(i, uv=2) for i in range(3)] + [hour(3, rain=70, mm=0.8, uv=2)] + \
            [hour(i, uv=2) for i in range(4, 6)]
    assert "12:00" in by_id(snap(hours), "umbrella", clock_format="24h").detail


def test_priority_sorted_and_limited():
    hours = [hour(i, feels=36, rain=80, mm=2, uv=9, gust=45, aqi=120) for i in range(6)]
    out = build_recommendations(snap(hours, humidity=90), now=NOW, limit=4)
    assert len(out) == 4
    order = {"high": 0, "medium": 1, "low": 2}
    ranks = [order[r.priority] for r in out]
    assert ranks == sorted(ranks)
