"""Controlled API payloads for browser tests, built with the app's own models
so their shape always matches the real API contract."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import WeatherPayload
from app.recommendations import build_recommendations
from app.weather import MockWeatherProvider, WeatherResponse, describe_code, visual_for

TZ = ZoneInfo("Asia/Kolkata")

SCENE_CODES = {
    "clear-day": (0, 12), "clear-night": (0, 22), "partly-cloudy-day": (2, 11), "partly-cloudy-night": (2, 21),
    "overcast": (3, 12), "fog": (45, 7), "drizzle": (53, 12), "rain": (63, 12), "heavy-rain": (65, 12),
    "thunderstorm": (95, 16), "snow": (73, 12), "unknown": (42, 12),
}


def weather_payload(code: int, hour: int, status: str = "ok", is_mock: bool = False) -> dict:
    """A live-looking Open-Meteo-shaped payload for a weather code at a local hour."""
    snap = asyncio.run(MockWeatherProvider().fetch(SimpleNamespace(city="Bengaluru"), "Asia/Kolkata"))
    cond, desc = describe_code(code)
    now = datetime.now(TZ).replace(hour=hour, minute=0, second=0, microsecond=0)
    snap.is_mock = is_mock
    snap.source = "Development preview (sample data)" if is_mock else "Open-Meteo"
    snap.attribution = None if is_mock else "Weather data by Open-Meteo.com (CC BY 4.0)"
    snap.current.weather_code = code
    snap.current.condition = cond
    snap.current.description = desc
    snap.sunrise = now.replace(hour=6, minute=10)
    snap.sunset = now.replace(hour=17, minute=59)
    snap.fetched_at = datetime.now(TZ) - (timedelta(hours=2) if status == "stale" else timedelta(minutes=3))
    resp = WeatherResponse(status=status, is_mock=is_mock, data=snap,
                           message="Weather service unavailable" if status == "stale" else None)
    payload = WeatherPayload(**resp.model_dump(), recommendations=build_recommendations(snap, now=now),
                             visual=visual_for(resp, now))
    return payload.model_dump(mode="json")


def unavailable_weather() -> dict:
    resp = WeatherResponse(status="unavailable", reason="no_location", message="Set your home location to see weather")
    return WeatherPayload(**resp.model_dump(), visual=visual_for(resp)).model_dump(mode="json")


def commute_payload(kind: str) -> dict:
    """Real CommuteStatus JSON produced by the monitor against a fake TomTom."""
    import tempfile
    from pathlib import Path

    import httpx

    from app.commute import CommuteMonitor, CommuteStore, TomTomProvider
    from test_commute import MONDAY, SATURDAY, Clock, at, cfg, tomtom

    clock = Clock(at(MONDAY, 6, 45))
    net = tomtom({"09:15": 24, "09:30": 26, "08:30": 49, "08:45": 52, "10:30": 29}, live_minutes=41, clock=clock)
    d = Path(tempfile.mkdtemp())
    provider = None if kind == "unconfigured" else TomTomProvider(httpx.AsyncClient(transport=net.transport), "k")
    m = CommuteMonitor(cfg(), provider, CommuteStore(d / "aura.db"), clock=clock,
                       missing="Live traffic needs a free TomTom API key (AURA_TOMTOM_API_KEY). See README → Commute.")
    m.REQUEST_SPACING = 0
    if provider:
        asyncio.run(m.tick())          # 06:45 forecast for every slot
        clock.now = at(MONDAY, 8)
        asyncio.run(m.tick())          # 08:00 live check + refresh
    if kind == "after_window":
        clock.now = at(MONDAY, 12)
    elif kind == "off_day":
        clock.now = at(SATURDAY, 9)
    status = m.status().model_dump(mode="json")
    m.store.close()
    return status
