"""Open-Meteo weather provider.

Free for non-commercial use (personal home automation is explicitly allowed),
no API key, < 10,000 calls/day. Data is CC BY 4.0 and must be attributed.
https://open-meteo.com/en/terms

Coordinates sent are the saved home location, already rounded to ~1 km.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .weather import (
    AirQuality,
    CurrentWeather,
    DailyForecast,
    HourlyForecast,
    WeatherLocation,
    WeatherProviderError,
    WeatherSnapshot,
    aqi_category,
    describe_code,
)

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)"

CURRENT_VARS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,is_day,precipitation,"
    "weather_code,wind_speed_10m,wind_gusts_10m,uv_index"
)
HOURLY_VARS = (
    "temperature_2m,apparent_temperature,precipitation_probability,precipitation,"
    "weather_code,wind_speed_10m,wind_gusts_10m,uv_index,is_day"
)
DAILY_VARS = (
    "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,"
    "precipitation_probability_max,precipitation_sum,uv_index_max"
)
FORECAST_HOURS = 30
FORECAST_DAYS = 7


def _opt_int(v) -> int | None:
    return None if v is None else int(round(v))


def _opt_float(v) -> float | None:
    return None if v is None else float(v)


def _at(series: dict, name: str, i: int):
    values = series.get(name)
    if not isinstance(values, list) or i >= len(values):
        return None
    return values[i]


def parse_forecast(data: dict, city: str, aq: dict | None = None) -> WeatherSnapshot:
    """Convert an Open-Meteo forecast (+ optional air-quality) response."""
    try:
        tz_name = data["timezone"]
        tz = ZoneInfo(tz_name)
    except (KeyError, ZoneInfoNotFoundError, ValueError) as exc:
        raise WeatherProviderError("Weather response had no valid timezone") from exc

    def local(ts: str) -> datetime:
        return datetime.fromisoformat(ts).replace(tzinfo=tz)

    try:
        cur = data["current"]
        hourly_raw = data["hourly"]
        daily_raw = data["daily"]

        aq_by_time: dict[str, int] = {}
        if aq:
            aq_h = aq.get("hourly") or {}
            for t, v in zip(aq_h.get("time") or [], aq_h.get("us_aqi") or []):
                if v is not None:
                    aq_by_time[t] = int(round(v))

        hourly: list[HourlyForecast] = []
        for i, t in enumerate(hourly_raw["time"]):
            code = _at(hourly_raw, "weather_code", i)
            temp = _at(hourly_raw, "temperature_2m", i)
            if temp is None or code is None:
                continue
            cond, _ = describe_code(code)
            hourly.append(HourlyForecast(
                time=local(t),
                temperature_c=temp,
                feels_like_c=_opt_float(_at(hourly_raw, "apparent_temperature", i)),
                condition=cond,
                precipitation_chance_pct=_opt_int(_at(hourly_raw, "precipitation_probability", i)),
                precipitation_mm=_opt_float(_at(hourly_raw, "precipitation", i)),
                wind_kph=_opt_float(_at(hourly_raw, "wind_speed_10m", i)),
                wind_gust_kph=_opt_float(_at(hourly_raw, "wind_gusts_10m", i)),
                uv_index=_opt_float(_at(hourly_raw, "uv_index", i)),
                us_aqi=aq_by_time.get(t),
                is_day=bool(_at(hourly_raw, "is_day", i)),
                weather_code=int(code),
            ))

        daily: list[DailyForecast] = []
        for i, d in enumerate(daily_raw["time"]):
            hi, lo = _at(daily_raw, "temperature_2m_max", i), _at(daily_raw, "temperature_2m_min", i)
            if hi is None or lo is None:
                continue
            cond, desc = describe_code(_at(daily_raw, "weather_code", i))
            sr, ss = _at(daily_raw, "sunrise", i), _at(daily_raw, "sunset", i)
            daily.append(DailyForecast(
                date=date.fromisoformat(d),
                condition=cond,
                description=desc,
                high_c=hi,
                low_c=lo,
                precipitation_chance_pct=_opt_int(_at(daily_raw, "precipitation_probability_max", i)),
                precipitation_mm=_opt_float(_at(daily_raw, "precipitation_sum", i)),
                uv_index_max=_opt_float(_at(daily_raw, "uv_index_max", i)),
                sunrise=local(sr) if sr else None,
                sunset=local(ss) if ss else None,
            ))

        code = cur.get("weather_code")
        cond, desc = describe_code(code)
        observed = local(cur["time"])
        hour_key = observed.replace(minute=0)
        chance = next((h.precipitation_chance_pct for h in hourly if h.time == hour_key), None)

        current = CurrentWeather(
            temperature_c=cur["temperature_2m"],
            feels_like_c=cur.get("apparent_temperature", cur["temperature_2m"]),
            condition=cond,
            description=desc,
            humidity_pct=int(round(cur["relative_humidity_2m"])),
            wind_kph=cur.get("wind_speed_10m") or 0.0,
            wind_gust_kph=_opt_float(cur.get("wind_gusts_10m")),
            uv_index=_opt_float(cur.get("uv_index")),
            precipitation_mm=cur.get("precipitation") or 0.0,
            precipitation_chance_pct=chance,
            is_day=bool(cur.get("is_day")),
            weather_code=None if code is None else int(code),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherProviderError("Weather response was incomplete") from exc

    if not daily:
        raise WeatherProviderError("Weather response had no daily forecast")

    air = None
    if aq:
        cur_aq = aq.get("current") or {}
        v = cur_aq.get("us_aqi")
        if v is None:
            # Just after the hour "current" can be empty while the hourly list has this hour's
            # value: use that (only this hour's or the next one's, never an older value).
            aq_h = aq.get("hourly") or {}
            want = cur_aq.get("time", "")[:13]
            for t, hv in zip(aq_h.get("time") or [], aq_h.get("us_aqi") or []):
                if hv is not None and (not want or t[:13] >= want):
                    v = hv
                    break
        if v is not None:
            v = int(round(v))
            air = AirQuality(us_aqi=v, category=aqi_category(v), pm2_5=_opt_float(cur_aq.get("pm2_5")))

    today = daily[0]
    return WeatherSnapshot(
        city=city,
        timezone=tz_name,
        observed_at=observed,
        fetched_at=datetime.now(tz),
        source="Open-Meteo",
        attribution=ATTRIBUTION,
        is_mock=False,
        current=current,
        high_c=today.high_c,
        low_c=today.low_c,
        sunrise=today.sunrise,
        sunset=today.sunset,
        hourly=hourly,
        daily=daily,
        air_quality=air,
    )


class OpenMeteoProvider:
    name = "open_meteo"
    is_mock = False

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def _get(self, url: str, params: dict) -> dict:
        res = await self.client.get(url, params=params)
        if res.status_code == 429:
            raise WeatherProviderError("Weather service rate limit reached")
        res.raise_for_status()
        data = res.json()
        if not isinstance(data, dict) or data.get("error"):
            raise WeatherProviderError(f"Weather service error: {data.get('reason', 'unknown')}")
        return data

    async def fetch(self, location: WeatherLocation, timezone: str) -> WeatherSnapshot:
        lat, lon = f"{location.latitude:.2f}", f"{location.longitude:.2f}"
        forecast_params = {
            "latitude": lat, "longitude": lon,
            "current": CURRENT_VARS, "hourly": HOURLY_VARS, "daily": DAILY_VARS,
            "timezone": "auto", "forecast_days": FORECAST_DAYS, "forecast_hours": FORECAST_HOURS,
            "wind_speed_unit": "kmh",
        }
        aq_params = {
            "latitude": lat, "longitude": lon, "current": "us_aqi,pm2_5", "hourly": "us_aqi",
            "timezone": "auto", "forecast_hours": FORECAST_HOURS,
        }
        forecast, aq = await asyncio.gather(
            self._get(FORECAST_URL, forecast_params),
            self._get(AIR_QUALITY_URL, aq_params),
            return_exceptions=True,
        )
        if isinstance(forecast, BaseException):
            if isinstance(forecast, httpx.TimeoutException):
                raise WeatherProviderError("Weather service timed out") from forecast
            if isinstance(forecast, WeatherProviderError):
                raise forecast
            if isinstance(forecast, (httpx.HTTPError, ValueError)):
                raise WeatherProviderError("Weather service unavailable") from forecast
            raise forecast
        if isinstance(aq, BaseException):
            # Air quality is optional; weather still works without it.
            log.warning("Air quality unavailable: %r", aq)
            aq = None
        return parse_forecast(forecast, location.city, aq)
