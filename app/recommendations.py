"""Turn a weather snapshot into short, explained advice for leaving the house.

Rules look at the *travel window* (now → ``window_hours`` ahead), not just the
current moment, so "rain at 4 PM" still means "take an umbrella" at 1 PM.

Pure functions only: no I/O, so the rules are easy to test and tune.
Traffic is never claimed: mode-of-transport advice is weather-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from .weather import Condition, HourlyForecast, WeatherSnapshot

Priority = Literal["high", "medium", "low"]
_RANK = {"high": 0, "medium": 1, "low": 2}

WET_CONDITIONS = {Condition.DRIZZLE, Condition.RAIN, Condition.THUNDERSTORM}

# Thresholds (metric).
RAIN_LIKELY_PCT = 50
RAIN_POSSIBLE_PCT = 30
RAIN_MM_UMBRELLA = 1.0      # total over the window
RAIN_MM_RAINCOAT = 5.0
HOT_FEELS_C = 32
WATER_FEELS_C = 30
WARM_FEELS_C = 28
COOL_FEELS_C = 15
COLD_FEELS_C = 8
LAYERS_SWING_C = 8
UV_HIGH = 6
UV_VERY_HIGH = 8
UV_MODERATE = 3
GUST_WINDY_KPH = 40
GUST_SEVERE_KPH = 60
GUST_UMBRELLA_KPH = 40      # umbrellas become impractical
AQI_SENSITIVE = 101
AQI_UNHEALTHY = 151

# Cycling comfort envelope.
BIKE_MAX_RAIN_PCT = 30
BIKE_MAX_GUST_KPH = 35
BIKE_MIN_FEELS_C = 5
BIKE_MAX_FEELS_C = 33
BIKE_HOURS = 3

NO_TRAFFIC = "Based on weather only; traffic isn't checked."


class Recommendation(BaseModel):
    id: str
    icon: str
    title: str
    detail: str
    priority: Priority


@dataclass
class _Window:
    hours: list[HourlyForecast]
    fmt: callable

    def feels(self, h: HourlyForecast) -> float:
        return h.feels_like_c if h.feels_like_c is not None else h.temperature_c

    def peak(self, key):
        best = None
        for h in self.hours:
            v = key(h)
            if v is not None and (best is None or v > best[0]):
                best = (v, h)
        return best

    def low(self, key):
        best = None
        for h in self.hours:
            v = key(h)
            if v is not None and (best is None or v < best[0]):
                best = (v, h)
        return best


def _time_formatter(clock_format: str):
    def fmt(t: datetime) -> str:
        if clock_format == "24h":
            return t.strftime("%H:%M")
        return f"{t.hour % 12 or 12} {'AM' if t.hour < 12 else 'PM'}"
    return fmt


def _window(snapshot: WeatherSnapshot, now: datetime, hours: int) -> list[HourlyForecast]:
    start = now.replace(minute=0, second=0, microsecond=0)
    end = now + timedelta(hours=hours)
    win = [h for h in snapshot.hourly if start <= h.time < end]
    if win:
        return win
    c = snapshot.current
    # No hourly data: fall back to current conditions as a one-hour window.
    return [HourlyForecast(
        time=start, temperature_c=c.temperature_c, feels_like_c=c.feels_like_c,
        condition=c.condition, precipitation_chance_pct=c.precipitation_chance_pct,
        precipitation_mm=c.precipitation_mm, wind_kph=c.wind_kph, wind_gust_kph=c.wind_gust_kph,
        uv_index=c.uv_index, is_day=c.is_day, weather_code=c.weather_code,
    )]


def build_recommendations(
    snapshot: WeatherSnapshot,
    *,
    now: datetime | None = None,
    window_hours: int = 6,
    clock_format: str = "12h",
    limit: int = 4,
) -> list[Recommendation]:
    now = now or snapshot.fetched_at
    w = _Window(_window(snapshot, now, window_hours), _time_formatter(clock_format))
    fmt = w.fmt
    cur = snapshot.current
    span = f"the next {window_hours} h"
    recs: list[Recommendation] = []

    def add(id, icon, priority, title, detail):  # noqa: A002
        recs.append(Recommendation(id=id, icon=icon, priority=priority, title=title, detail=detail))

    # --- severe weather ---------------------------------------------------
    storm = next((h for h in w.hours if h.condition == Condition.THUNDERSTORM), None)
    if storm or cur.condition == Condition.THUNDERSTORM:
        when = "now" if cur.condition == Condition.THUNDERSTORM else f"around {fmt(storm.time)}"
        add("storm", "storm", "high", "Thunderstorms expected",
            f"Storms likely {when}. Avoid open areas and delay trips if you can.")

    gust = w.peak(lambda h: h.wind_gust_kph)
    if gust and gust[0] >= GUST_SEVERE_KPH:
        add("wind", "wind", "high", "Strong winds",
            f"Gusts up to {round(gust[0])} km/h around {fmt(gust[1].time)}. Secure loose items.")
    elif gust and gust[0] >= GUST_WINDY_KPH:
        add("wind", "wind", "medium", "Windy",
            f"Gusts up to {round(gust[0])} km/h around {fmt(gust[1].time)}.")

    # --- rain ---------------------------------------------------------------
    raining_now = cur.condition in WET_CONDITIONS or cur.precipitation_mm > 0.1
    chance = w.peak(lambda h: h.precipitation_chance_pct)
    total_mm = sum(h.precipitation_mm or 0 for h in w.hours)
    first_wet = next((h for h in w.hours if (h.precipitation_chance_pct or 0) >= RAIN_LIKELY_PCT
                      or (h.precipitation_mm or 0) >= 0.5 or h.condition in WET_CONDITIONS), None)
    rain_likely = raining_now or (chance and chance[0] >= RAIN_LIKELY_PCT) or total_mm >= RAIN_MM_UMBRELLA

    if rain_likely:
        if raining_now:
            detail = "It's raining now"
        elif first_wet is not None:
            pct = first_wet.precipitation_chance_pct
            detail = f"Rain likely from {fmt(first_wet.time)}" + (f" ({pct}% chance)" if pct else "")
        else:
            detail = f"Rain expected in {span}"
        if total_mm >= 0.5:
            detail += f", about {total_mm:.0f} mm over {span}" if total_mm >= 1 else f", light amounts over {span}"
        windy = gust is not None and gust[0] >= GUST_UMBRELLA_KPH
        if total_mm >= RAIN_MM_RAINCOAT or windy:
            why = "too windy for an umbrella" if windy else "heavy rain"
            add("raincoat", "raincoat", "high", "Wear a raincoat", f"{detail}. A raincoat beats an umbrella: {why}.")
        else:
            add("umbrella", "umbrella", "high", "Take an umbrella", f"{detail}.")
    elif chance and chance[0] >= RAIN_POSSIBLE_PCT:
        add("umbrella-maybe", "umbrella", "low", "Compact umbrella, just in case",
            f"Up to {chance[0]}% chance of rain around {fmt(chance[1].time)}.")

    if any(h.condition == Condition.SNOW for h in w.hours):
        add("snow", "snow", "high", "Snow expected", "Wear boots with grip and allow extra travel time.")

    if any(h.condition == Condition.FOG for h in w.hours):
        fog = next(h for h in w.hours if h.condition == Condition.FOG)
        when = "now" if fog.time <= now else f"around {fmt(fog.time)}"
        add("fog", "fog", "medium", "Low visibility", f"Fog {when}. Drive slowly with low beams on.")

    # --- temperature & clothing ------------------------------------------
    hi = w.peak(w.feels)
    lo = w.low(w.feels)
    if hi and lo:
        swing = hi[0] - lo[0]
        if lo[0] <= COLD_FEELS_C:
            add("coat", "coat", "high", "Wear a warm coat",
                f"Feels as cold as {round(lo[0])}° around {fmt(lo[1].time)}.")
        elif swing >= LAYERS_SWING_C and lo[0] <= 20:
            add("layers", "jacket", "medium", "Dress in layers",
                f"Feels from {round(lo[0])}° to {round(hi[0])}° over {span}.")
        elif lo[0] <= COOL_FEELS_C:
            add("jacket", "jacket", "medium", "Bring a light jacket",
                f"Feels like {round(lo[0])}° around {fmt(lo[1].time)}.")
        elif hi[0] >= WARM_FEELS_C:
            add("light-clothes", "shirt", "low", "Dress light",
                f"Feels up to {round(hi[0])}°. Breathable clothing recommended.")

        if hi[0] >= WATER_FEELS_C:
            add("water", "water", "high" if hi[0] >= HOT_FEELS_C + 3 else "medium", "Carry a water bottle",
                f"Feels up to {round(hi[0])}° around {fmt(hi[1].time)}. Stay hydrated.")
        elif hi[0] >= WARM_FEELS_C - 1 and cur.humidity_pct >= 75:
            add("water", "water", "low", "Carry a water bottle",
                f"Warm and humid ({cur.humidity_pct}%). You'll lose more water than you notice.")

    # --- UV ---------------------------------------------------------------
    uv = w.peak(lambda h: h.uv_index if h.is_day else None)
    if uv and uv[0] >= UV_VERY_HIGH:
        add("uv", "sun", "high", "Sunscreen and sunglasses",
            f"Very high UV ({round(uv[0])}) around {fmt(uv[1].time)}. Seek shade at midday.")
    elif uv and uv[0] >= UV_HIGH:
        add("uv", "sun", "medium", "Sunscreen and sunglasses",
            f"High UV ({round(uv[0])}) around {fmt(uv[1].time)}.")
    elif uv and uv[0] >= UV_MODERATE:
        add("sunglasses", "sunglasses", "low", "Sunglasses",
            f"Moderate UV ({round(uv[0])}) around {fmt(uv[1].time)}.")

    # --- air quality ------------------------------------------------------
    aqi = w.peak(lambda h: h.us_aqi)
    aqi_val = max(filter(None, [aqi[0] if aqi else None,
                                snapshot.air_quality.us_aqi if snapshot.air_quality else None]), default=None)
    if aqi_val is not None and aqi_val >= AQI_UNHEALTHY:
        add("air", "air", "high", "Poor air quality",
            f"US AQI up to {aqi_val}. Consider an N95 mask and limit time outdoors.")
    elif aqi_val is not None and aqi_val >= AQI_SENSITIVE:
        add("air", "air", "medium", "Air quality: take care",
            f"US AQI up to {aqi_val}. Sensitive groups should limit exertion outdoors.")

    # --- bike vs car (weather only) ---------------------------------------
    near = w.hours[:BIKE_HOURS]
    has_info = any(h.precipitation_chance_pct is not None for h in near) and any(
        h.wind_gust_kph is not None or h.wind_kph is not None for h in near)
    if has_info:
        reasons = []
        near_rain = max((h.precipitation_chance_pct or 0) for h in near)
        near_mm = sum(h.precipitation_mm or 0 for h in near)
        near_gust = max((h.wind_gust_kph or h.wind_kph or 0) for h in near)
        near_feels = [w.feels(h) for h in near]
        if raining_now or near_rain >= BIKE_MAX_RAIN_PCT or near_mm >= 0.3:
            reasons.append(f"rain likely ({near_rain}%)" if near_rain else "wet roads")
        if any(h.condition in {Condition.THUNDERSTORM, Condition.SNOW, Condition.FOG} for h in near):
            reasons.append("poor conditions")
        if near_gust >= BIKE_MAX_GUST_KPH:
            reasons.append(f"gusts {round(near_gust)} km/h")
        if max(near_feels) > BIKE_MAX_FEELS_C:
            reasons.append(f"heat (feels {round(max(near_feels))}°)")
        if min(near_feels) < BIKE_MIN_FEELS_C:
            reasons.append(f"cold (feels {round(min(near_feels))}°)")
        if aqi_val is not None and aqi_val >= AQI_SENSITIVE:
            reasons.append(f"air quality (AQI {aqi_val})")
        if reasons:
            add("car", "car", "medium", "Car or transit over bike",
                f"Next {len(near)} h: {', '.join(reasons)}. {NO_TRAFFIC}")
        else:
            add("bike", "bike", "medium", "Good conditions to cycle",
                f"Dry and calm for the next {len(near)} h, feels {round(max(near_feels))}°. {NO_TRAFFIC}")

    if not [r for r in recs if r.id not in {"bike", "car"}]:
        add("pleasant", "check", "low", "Pleasant conditions",
            f"No rain, heat or UV concerns in {span}.")

    recs.sort(key=lambda r: _RANK[r.priority])  # stable: keeps rule order within a tier
    return recs[:limit]
