"""Weather models, providers and a caching service.

Providers:

* ``open_meteo`` - live data from Open-Meteo (see ``app/open_meteo.py``).
* ``mock``       - a fixed sample, always flagged ``is_mock=True`` so the UI
                   labels it as a development preview. Never presented as live.
* ``none``       - weather disabled.

``WeatherService`` adds caching (memory + ``data/weather_cache.json``), a retry
back-off after failures, and serves the last good data, clearly marked
``stale``, while the provider is unavailable. It never invents data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from .storage import write_json_atomic

log = logging.getLogger(__name__)


class Condition(StrEnum):
    CLEAR = "clear"
    PARTLY_CLOUDY = "partly_cloudy"
    CLOUDY = "cloudy"
    FOG = "fog"
    DRIZZLE = "drizzle"
    RAIN = "rain"
    THUNDERSTORM = "thunderstorm"
    SNOW = "snow"


class CurrentWeather(BaseModel):
    temperature_c: float
    feels_like_c: float
    condition: Condition
    description: str
    humidity_pct: int
    wind_kph: float
    wind_gust_kph: float | None = None
    uv_index: float | None = None
    precipitation_mm: float = 0.0
    precipitation_chance_pct: int | None = None
    is_day: bool
    weather_code: int | None = None


class HourlyForecast(BaseModel):
    time: datetime
    temperature_c: float
    feels_like_c: float | None = None
    condition: Condition
    precipitation_chance_pct: int | None = None
    precipitation_mm: float | None = None
    wind_kph: float | None = None
    wind_gust_kph: float | None = None
    uv_index: float | None = None
    us_aqi: int | None = None
    is_day: bool
    weather_code: int | None = None


class DailyForecast(BaseModel):
    date: date
    condition: Condition
    description: str
    high_c: float
    low_c: float
    precipitation_chance_pct: int | None = None
    precipitation_mm: float | None = None
    uv_index_max: float | None = None
    sunrise: datetime | None = None
    sunset: datetime | None = None


class AirQuality(BaseModel):
    us_aqi: int
    category: str
    pm2_5: float | None = None


class WeatherSnapshot(BaseModel):
    city: str
    timezone: str
    observed_at: datetime
    fetched_at: datetime
    source: str
    attribution: str | None = None
    is_mock: bool
    current: CurrentWeather
    high_c: float
    low_c: float
    sunrise: datetime | None = None
    sunset: datetime | None = None
    hourly: list[HourlyForecast]
    daily: list[DailyForecast] = []
    air_quality: AirQuality | None = None


class WeatherResponse(BaseModel):
    status: Literal["ok", "stale", "unavailable", "error"]
    reason: Literal["no_location", "disabled", "provider_error"] | None = None
    is_mock: bool = False
    message: str | None = None
    data: WeatherSnapshot | None = None


class WeatherProviderError(Exception):
    """Raised by providers when data cannot be fetched or parsed."""


class WeatherLocation(Protocol):
    city: str
    latitude: float
    longitude: float
    timezone: str | None


class WeatherProvider(Protocol):
    name: str
    is_mock: bool

    async def fetch(self, location: WeatherLocation, timezone: str) -> WeatherSnapshot: ...


# ---------- WMO weather codes (used by Open-Meteo) ----------

WMO_CODES: dict[int, tuple[Condition, str]] = {
    0: (Condition.CLEAR, "Clear"),
    1: (Condition.CLEAR, "Mainly clear"),
    2: (Condition.PARTLY_CLOUDY, "Partly cloudy"),
    3: (Condition.CLOUDY, "Overcast"),
    45: (Condition.FOG, "Fog"),
    48: (Condition.FOG, "Freezing fog"),
    51: (Condition.DRIZZLE, "Light drizzle"),
    53: (Condition.DRIZZLE, "Drizzle"),
    55: (Condition.DRIZZLE, "Heavy drizzle"),
    56: (Condition.DRIZZLE, "Freezing drizzle"),
    57: (Condition.DRIZZLE, "Freezing drizzle"),
    61: (Condition.RAIN, "Light rain"),
    63: (Condition.RAIN, "Rain"),
    65: (Condition.RAIN, "Heavy rain"),
    66: (Condition.RAIN, "Freezing rain"),
    67: (Condition.RAIN, "Heavy freezing rain"),
    71: (Condition.SNOW, "Light snow"),
    73: (Condition.SNOW, "Snow"),
    75: (Condition.SNOW, "Heavy snow"),
    77: (Condition.SNOW, "Snow grains"),
    80: (Condition.RAIN, "Light showers"),
    81: (Condition.RAIN, "Showers"),
    82: (Condition.RAIN, "Violent showers"),
    85: (Condition.SNOW, "Snow showers"),
    86: (Condition.SNOW, "Heavy snow showers"),
    95: (Condition.THUNDERSTORM, "Thunderstorm"),
    96: (Condition.THUNDERSTORM, "Thunderstorm with hail"),
    99: (Condition.THUNDERSTORM, "Severe thunderstorm with hail"),
}

SEVERE_CODES = {65, 67, 75, 82, 86, 95, 96, 99}

# ---------- animation scenes -------------------------------------------------
# The weather panel's background animation is chosen from the provider's own
# weather code (never from temperature). Day/night comes from real sunrise and
# sunset times at the home location.

Scene = Literal[
    "clear-day", "clear-night", "partly-cloudy-day", "partly-cloudy-night", "overcast",
    "drizzle", "rain", "heavy-rain", "thunderstorm", "snow", "fog", "unknown",
]

_SCENE_BY_CODE: dict[int, str] = {
    0: "clear", 1: "clear", 2: "partly-cloudy", 3: "overcast",
    45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle", 56: "drizzle", 57: "drizzle",
    61: "rain", 63: "rain", 66: "rain", 80: "rain", 81: "rain",
    65: "heavy-rain", 67: "heavy-rain", 82: "heavy-rain",
    71: "snow", 73: "snow", 75: "snow", 77: "snow", 85: "snow", 86: "snow",
    95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm",
}
_SCENE_BY_CONDITION: dict[str, str] = {
    "clear": "clear", "partly_cloudy": "partly-cloudy", "cloudy": "overcast", "fog": "fog",
    "drizzle": "drizzle", "rain": "rain", "thunderstorm": "thunderstorm", "snow": "snow",
}


def scene_for(code: int | None, condition: "Condition | str | None", is_day: bool) -> str:
    """Map a weather code (preferred) or condition to an animation scene."""
    if code is not None:
        base = _SCENE_BY_CODE.get(int(code), "unknown")
    elif condition is not None:
        base = _SCENE_BY_CONDITION.get(str(condition), "unknown")
    else:
        base = "unknown"
    if base in ("clear", "partly-cloudy"):
        return f"{base}-{'day' if is_day else 'night'}"
    return base


def is_daytime(now: datetime, sunrise: datetime | None, sunset: datetime | None, fallback: bool) -> bool:
    """Day if local time is between sunrise and sunset (same-day times of day)."""
    if sunrise is None or sunset is None:
        return fallback
    tz = now.tzinfo
    rise = sunrise.astimezone(tz).time() if tz else sunrise.time()
    set_ = sunset.astimezone(tz).time() if tz else sunset.time()
    if rise >= set_:  # polar edge cases / bad data
        return fallback
    return rise <= now.time() < set_


class WeatherVisual(BaseModel):
    scene: str
    is_day: bool
    # "live": animate. "static": data is sample or out of date, so the panel
    # must not suggest live conditions. "none": no data to show.
    motion: Literal["live", "static", "none"]
    reason: str | None = None


def visual_for(response: "WeatherResponse", now: datetime | None = None) -> WeatherVisual:
    snap = response.data
    if snap is None:
        return WeatherVisual(scene="unknown", is_day=True, motion="none", reason=response.status)
    now = now or datetime.now(ZoneInfo(snap.timezone))
    day = is_daytime(now, snap.sunrise, snap.sunset, snap.current.is_day)
    scene = scene_for(snap.current.weather_code, snap.current.condition, day)
    if snap.is_mock:
        return WeatherVisual(scene=scene, is_day=day, motion="static", reason="sample")
    if response.status == "stale":
        return WeatherVisual(scene=scene, is_day=day, motion="static", reason="stale")
    return WeatherVisual(scene=scene, is_day=day, motion="live")


def describe_code(code: int | None) -> tuple[Condition, str]:
    if code is None:
        return Condition.CLOUDY, "Unknown"
    return WMO_CODES.get(int(code), (Condition.CLOUDY, "Unknown"))


def aqi_category(us_aqi: int) -> str:
    if us_aqi <= 50:
        return "Good"
    if us_aqi <= 100:
        return "Moderate"
    if us_aqi <= 150:
        return "Unhealthy for sensitive groups"
    if us_aqi <= 200:
        return "Unhealthy"
    if us_aqi <= 300:
        return "Very unhealthy"
    return "Hazardous"


# ---------- mock provider ----------

class MockWeatherProvider:
    """Static sample for development previews. Never presented as live."""

    name = "mock"
    is_mock = True

    async def fetch(self, location: WeatherLocation, timezone: str) -> WeatherSnapshot:
        tz = ZoneInfo(timezone)
        real_now = datetime.now(tz)
        now = real_now.replace(minute=0, second=0, microsecond=0)
        sample = [
            (29, 33, Condition.PARTLY_CLOUDY, 20, 0.0, 7),
            (28, 32, Condition.CLOUDY, 35, 0.1, 5),
            (27, 30, Condition.RAIN, 65, 1.2, 3),
            (26, 28, Condition.RAIN, 70, 2.4, 1),
            (26, 28, Condition.CLOUDY, 40, 0.2, 0),
            (25, 27, Condition.PARTLY_CLOUDY, 15, 0.0, 0),
            (25, 27, Condition.PARTLY_CLOUDY, 10, 0.0, 0),
        ]
        hourly = []
        for i, (temp, feels, cond, rain, mm, uv) in enumerate(sample):
            t = now + timedelta(hours=i)
            hourly.append(HourlyForecast(
                time=t, temperature_c=temp, feels_like_c=feels, condition=cond,
                precipitation_chance_pct=rain, precipitation_mm=mm, wind_kph=14,
                wind_gust_kph=24, uv_index=uv if 6 <= t.hour < 18 else 0, us_aqi=62,
                is_day=6 <= t.hour < 18,
            ))
        today = now.date()
        daily = []
        for i, (cond, desc, hi, lo, rain) in enumerate([
            (Condition.RAIN, "Showers", 31, 25, 70),
            (Condition.THUNDERSTORM, "Thunderstorm", 30, 24, 80),
            (Condition.PARTLY_CLOUDY, "Partly cloudy", 32, 25, 20),
            (Condition.CLEAR, "Clear", 33, 26, 5),
            (Condition.CLOUDY, "Overcast", 30, 25, 40),
        ]):
            d = today + timedelta(days=i)
            daily.append(DailyForecast(
                date=d, condition=cond, description=desc, high_c=hi, low_c=lo,
                precipitation_chance_pct=rain, precipitation_mm=None, uv_index_max=8,
                sunrise=datetime(d.year, d.month, d.day, 6, 15, tzinfo=tz),
                sunset=datetime(d.year, d.month, d.day, 18, 5, tzinfo=tz),
            ))
        return WeatherSnapshot(
            city=location.city,
            timezone=timezone,
            observed_at=now,
            fetched_at=real_now,
            source="Development preview (sample data)",
            is_mock=True,
            current=CurrentWeather(
                temperature_c=29, feels_like_c=33, condition=Condition.PARTLY_CLOUDY,
                description="Partly cloudy", humidity_pct=74, wind_kph=14, wind_gust_kph=24,
                uv_index=hourly[0].uv_index, precipitation_chance_pct=20,
                is_day=6 <= now.hour < 18,
            ),
            high_c=31,
            low_c=25,
            sunrise=daily[0].sunrise,
            sunset=daily[0].sunset,
            hourly=hourly,
            daily=daily,
            air_quality=AirQuality(us_aqi=62, category=aqi_category(62), pm2_5=18),
        )


# ---------- caching service ----------

class CacheEntry(BaseModel):
    key: str
    fetched_ts: float  # unix seconds
    snapshot: WeatherSnapshot


class WeatherService:
    def __init__(
        self,
        provider: WeatherProvider | None,
        cache_path: Path | None,
        *,
        refresh_seconds: int = 900,
        retry_seconds: int = 120,
        max_stale_hours: float = 6,
        clock=time.time,
    ) -> None:
        self.provider = provider
        self.cache_path = cache_path
        self.refresh_seconds = refresh_seconds
        self.retry_seconds = retry_seconds
        self.max_stale_seconds = max_stale_hours * 3600
        self.clock = clock
        self._lock = asyncio.Lock()
        self._entry: CacheEntry | None = self._load()
        self._failure: tuple[str, float, str] | None = None  # key, ts, message

    def _load(self) -> CacheEntry | None:
        if not self.cache_path or not self.cache_path.exists():
            return None
        try:
            return CacheEntry.model_validate_json(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            log.warning("Ignoring unreadable weather cache %s", self.cache_path)
            return None

    def _persist(self, entry: CacheEntry) -> None:
        if not self.cache_path or entry.snapshot.is_mock:
            return
        try:
            write_json_atomic(self.cache_path, entry.model_dump(mode="json"))
        except OSError:
            log.exception("Could not write weather cache")

    @staticmethod
    def _key(provider: WeatherProvider, loc: WeatherLocation) -> str:
        return f"{provider.name}:{loc.latitude:.2f},{loc.longitude:.2f}"

    def _trim(self, snap: WeatherSnapshot) -> WeatherSnapshot:
        """Drop hours/days that are already in the past (matters for cached data)."""
        tz = ZoneInfo(snap.timezone)
        now = datetime.fromtimestamp(self.clock(), tz)
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        hourly = [h for h in snap.hourly if h.time >= hour_start][:24]
        daily = [d for d in snap.daily if d.date >= now.date()]
        update: dict = {"hourly": hourly, "daily": daily}
        if daily and daily[0].date == now.date():
            # After midnight the snapshot's "today" fields would describe
            # yesterday; take them from today's forecast entry instead.
            today = daily[0]
            update.update(high_c=today.high_c, low_c=today.low_c, sunrise=today.sunrise, sunset=today.sunset)
        return snap.model_copy(update=update)

    def _ok(self, entry: CacheEntry) -> WeatherResponse:
        return WeatherResponse(status="ok", is_mock=entry.snapshot.is_mock, data=self._trim(entry.snapshot))

    def _stale(self, entry: CacheEntry, message: str) -> WeatherResponse:
        return WeatherResponse(
            status="stale", reason="provider_error", is_mock=entry.snapshot.is_mock,
            message=message, data=self._trim(entry.snapshot),
        )

    async def get(self, location: WeatherLocation | None, timezone: str) -> WeatherResponse:
        if self.provider is None:
            return WeatherResponse(status="unavailable", reason="disabled", message="Weather is turned off")
        if location is None:
            return WeatherResponse(
                status="unavailable", reason="no_location", message="Set your home location to see weather"
            )

        key = self._key(self.provider, location)
        async with self._lock:
            now = self.clock()
            entry = self._entry if self._entry and self._entry.key == key else None
            age = now - entry.fetched_ts if entry else None

            if entry and age < self.refresh_seconds:
                return self._ok(entry)

            if self._failure and self._failure[0] == key and now - self._failure[1] < self.retry_seconds:
                return self._fallback(entry, age, self._failure[2])

            try:
                snap = await self.provider.fetch(location, location.timezone or timezone)
            except WeatherProviderError as exc:
                log.warning("Weather fetch failed: %s", exc)
                self._failure = (key, now, str(exc))
                return self._fallback(entry, age, str(exc))

            self._failure = None
            self._entry = CacheEntry(key=key, fetched_ts=now, snapshot=snap)
            self._persist(self._entry)
            return self._ok(self._entry)

    def _fallback(self, entry: CacheEntry | None, age: float | None, message: str) -> WeatherResponse:
        if entry is not None and age is not None and age < self.max_stale_seconds:
            return self._stale(entry, message)
        return WeatherResponse(status="error", reason="provider_error", message=message)
