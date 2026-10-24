"""Sun & Moon for the weather card. Pure Python, no dependencies, no network.

Moon (computed here, from Jean Meeus, *Astronomical Algorithms*, 2nd ed.):

* Phase instants (new moon, first quarter, full moon, last quarter) use
  chapter 49: the mean phase plus the periodic and planetary corrections.
  The book quotes an accuracy of a few seconds to about a minute; AURA shows
  dates, so that is far more than enough.
* The illuminated fraction uses the phase angle i from chapter 48 (eq. 48.4,
  from the mean elongation D and the anomalies M, M'): k = (1 + cos i) / 2.
* Waxing/waning comes from the elongation λ_moon - λ_sun, with the Moon's
  longitude from the main terms of chapter 47 and the Sun's from chapter 25
  (low precision). Waxing means the elongation is between 0° and 180°.
* Age is the time since the previous new moon instant.
* The phase name follows the almanac convention (as used by timeanddate.com):
  the four principal phases are named on the local calendar day on which their
  instant falls; on other days the Moon is a crescent or gibbous, waxing or
  waning.

Orientation: the lit limb faces the Sun. AURA uses the simple hemisphere rule:
from the northern hemisphere a waxing Moon is lit on the right, from the
southern hemisphere on the left (mirrored). It does not rotate the disc by the
bright-limb/parallactic angle, so near the equator the real crescent appears
tilted ("boat-shaped") compared with the picture.

Sun: sunrise and sunset come from the weather provider (Open-Meteo) for the
home location; this module only derives day length, before-sunrise / daytime /
after-sunset, and how far along the sunrise -> sunset arc the Sun is now. The
dashboard re-derives status and arc position in the browser (static/js/sky.js)
from these same times and the page's own clock, so the tile, the detail view
and the clock never disagree; the values here describe ``computed_at``.
Nothing is invented: without provider times the Sun part is ``None``.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

SYNODIC_MONTH = 29.530588861
DELTA_T_SECONDS = 69.0      # TT - UT around 2020-2030; shifts phase instants by about a minute
_UNIX_EPOCH_JD = 2440587.5

PhaseName = Literal[
    "New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
    "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent",
]
PRINCIPAL: dict[float, PhaseName] = {
    0.0: "New Moon", 0.25: "First Quarter", 0.5: "Full Moon", 0.75: "Last Quarter",
}


def _sin(deg: float) -> float:
    return math.sin(math.radians(deg))


def _cos(deg: float) -> float:
    return math.cos(math.radians(deg))


def julian_day(t: datetime) -> float:
    """Julian Day (UT) of an aware datetime."""
    if t.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return t.timestamp() / 86400.0 + _UNIX_EPOCH_JD


def from_julian_day(jd: float) -> datetime:
    return datetime.fromtimestamp((jd - _UNIX_EPOCH_JD) * 86400.0, timezone.utc)


# ---------- phase instants (Meeus chapter 49) ----------

def phase_jde(k: float) -> float:
    """Julian Ephemeris Day (TT) of a lunar phase. k is an integer for new
    moon, +0.25 first quarter, +0.5 full moon, +0.75 last quarter
    (k = 0 is the new moon of 2000 January 6)."""
    frac = round((k % 1) * 4) / 4
    T = k / 1236.85
    jde = (2451550.09766 + 29.530588861 * k + 0.00015437 * T**2
           - 0.000000150 * T**3 + 0.00000000073 * T**4)
    E = 1 - 0.002516 * T - 0.0000074 * T**2
    M = 2.5534 + 29.10535670 * k - 0.0000014 * T**2 - 0.00000011 * T**3
    Mp = (201.5643 + 385.81693528 * k + 0.0107582 * T**2 + 0.00001238 * T**3
          - 0.000000058 * T**4)
    F = (160.7108 + 390.67050284 * k - 0.0016118 * T**2 - 0.00000227 * T**3
         + 0.000000011 * T**4)
    O = 124.7746 - 1.56375588 * k + 0.0020672 * T**2 + 0.00000215 * T**3

    if frac in (0.0, 0.5):
        new = frac == 0.0
        c = ((-0.40720 if new else -0.40614) * _sin(Mp)
             + (0.17241 if new else 0.17302) * E * _sin(M)
             + (0.01608 if new else 0.01614) * _sin(2 * Mp)
             + (0.01039 if new else 0.01043) * _sin(2 * F)
             + (0.00739 if new else 0.00734) * E * _sin(Mp - M)
             - (0.00514 if new else 0.00515) * E * _sin(Mp + M)
             + (0.00208 if new else 0.00209) * E * E * _sin(2 * M)
             - 0.00111 * _sin(Mp - 2 * F)
             - 0.00057 * _sin(Mp + 2 * F)
             + 0.00056 * E * _sin(2 * Mp + M)
             - 0.00042 * _sin(3 * Mp)
             + 0.00042 * E * _sin(M + 2 * F)
             + 0.00038 * E * _sin(M - 2 * F)
             - 0.00024 * E * _sin(2 * Mp - M)
             - 0.00017 * _sin(O)
             - 0.00007 * _sin(Mp + 2 * M)
             + 0.00004 * _sin(2 * Mp - 2 * F)
             + 0.00004 * _sin(3 * M)
             + 0.00003 * _sin(Mp + M - 2 * F)
             + 0.00003 * _sin(2 * Mp + 2 * F)
             - 0.00003 * _sin(Mp + M + 2 * F)
             + 0.00003 * _sin(Mp - M + 2 * F)
             - 0.00002 * _sin(Mp - M - 2 * F)
             - 0.00002 * _sin(3 * Mp + M)
             + 0.00002 * _sin(4 * Mp))
    else:
        c = (-0.62801 * _sin(Mp)
             + 0.17172 * E * _sin(M)
             - 0.01183 * E * _sin(Mp + M)
             + 0.00862 * _sin(2 * Mp)
             + 0.00804 * _sin(2 * F)
             + 0.00454 * E * _sin(Mp - M)
             + 0.00204 * E * E * _sin(2 * M)
             - 0.00180 * _sin(Mp - 2 * F)
             - 0.00070 * _sin(Mp + 2 * F)
             - 0.00040 * _sin(3 * Mp)
             - 0.00034 * E * _sin(2 * Mp - M)
             + 0.00032 * E * _sin(M + 2 * F)
             + 0.00032 * E * _sin(M - 2 * F)
             - 0.00028 * E * E * _sin(Mp + 2 * M)
             + 0.00027 * E * _sin(2 * Mp + M)
             - 0.00017 * _sin(O)
             - 0.00005 * _sin(Mp - M - 2 * F)
             + 0.00004 * _sin(2 * Mp + 2 * F)
             - 0.00004 * _sin(Mp + M + 2 * F)
             + 0.00004 * _sin(Mp - 2 * M)
             + 0.00003 * _sin(Mp + M - 2 * F)
             + 0.00003 * _sin(3 * M)
             + 0.00002 * _sin(2 * Mp - 2 * F)
             + 0.00002 * _sin(Mp - M + 2 * F)
             - 0.00002 * _sin(3 * Mp + M))
        W = (0.00306 - 0.00038 * E * _cos(M) + 0.00026 * _cos(Mp) - 0.00002 * _cos(Mp - M)
             + 0.00002 * _cos(Mp + M) + 0.00002 * _cos(2 * F))
        c += W if frac == 0.25 else -W

    planetary = sum(a * _sin(arg) for a, arg in (
        (0.000325, 299.77 + 0.107408 * k - 0.009173 * T**2),
        (0.000165, 251.88 + 0.016321 * k),
        (0.000164, 251.83 + 26.651886 * k),
        (0.000126, 349.42 + 36.412478 * k),
        (0.000110, 84.66 + 18.206239 * k),
        (0.000062, 141.74 + 53.303771 * k),
        (0.000060, 207.14 + 2.453732 * k),
        (0.000056, 154.84 + 7.306860 * k),
        (0.000047, 34.52 + 27.261239 * k),
        (0.000042, 207.19 + 0.121824 * k),
        (0.000040, 291.34 + 1.844379 * k),
        (0.000037, 161.72 + 24.198154 * k),
        (0.000035, 239.56 + 25.513099 * k),
        (0.000023, 331.55 + 3.592518 * k),
    ))
    return jde + c + planetary


def phase_time(k: float) -> datetime:
    """UTC instant of the lunar phase k (see ``phase_jde``)."""
    return from_julian_day(phase_jde(k) - DELTA_T_SECONDS / 86400.0)


def _k_near(t: datetime) -> int:
    return math.floor((julian_day(t) - 2451550.09766) / SYNODIC_MONTH)


def previous_new_moon(t: datetime) -> datetime:
    k = _k_near(t) + 1
    while phase_time(k) > t:
        k -= 1
    return phase_time(k)


def phases_between(start: datetime, end: datetime) -> list[tuple[PhaseName, datetime]]:
    """Principal phases with instants in [start, end), in time order."""
    out = []
    k0 = _k_near(start) - 1
    for i in range(int((end - start).days / SYNODIC_MONTH * 4) + 12):
        k = k0 + i / 4
        t = phase_time(k)
        if t >= end:
            break
        if t >= start:
            out.append((PRINCIPAL[round((k % 1) * 4) / 4], t))
    return out


# ---------- position, elongation and illumination ----------

def _centuries(t: datetime) -> float:
    return (julian_day(t) + DELTA_T_SECONDS / 86400.0 - 2451545.0) / 36525.0


def sun_longitude(t: datetime) -> float:
    """Apparent geocentric ecliptic longitude of the Sun, degrees (Meeus ch. 25, low precision)."""
    T = _centuries(t)
    L0 = 280.46646 + 36000.76983 * T + 0.0003032 * T**2
    M = 357.52911 + 35999.05029 * T - 0.0001537 * T**2
    C = ((1.914602 - 0.004817 * T - 0.000014 * T**2) * _sin(M)
         + (0.019993 - 0.000101 * T) * _sin(2 * M) + 0.000289 * _sin(3 * M))
    omega = 125.04 - 1934.136 * T
    return (L0 + C - 0.00569 - 0.00478 * _sin(omega)) % 360


def _moon_arguments(T: float) -> tuple[float, float, float, float, float, float]:
    Lp = 218.3164477 + 481267.88123421 * T - 0.0015786 * T**2 + T**3 / 538841 - T**4 / 65194000
    D = 297.8501921 + 445267.1114034 * T - 0.0018819 * T**2 + T**3 / 545868 - T**4 / 113065000
    M = 357.5291092 + 35999.0502909 * T - 0.0001536 * T**2 + T**3 / 24490000
    Mp = 134.9633964 + 477198.8675055 * T + 0.0087414 * T**2 + T**3 / 69699 - T**4 / 14712000
    F = 93.2720950 + 483202.0175233 * T - 0.0036539 * T**2 - T**3 / 3526000 + T**4 / 863310000
    E = 1 - 0.002516 * T - 0.0000074 * T**2
    return Lp, D, M, Mp, F, E


# Main periodic terms for the Moon's longitude (Meeus table 47.A): D, M, M', F, coefficient (1e-6 deg).
_MOON_LON_TERMS = (
    (0, 0, 1, 0, 6288774), (2, 0, -1, 0, 1274027), (2, 0, 0, 0, 658314), (0, 0, 2, 0, 213618),
    (0, 1, 0, 0, -185116), (0, 0, 0, 2, -114332), (2, 0, -2, 0, 58793), (2, -1, -1, 0, 57066),
    (2, 0, 1, 0, 53322), (2, -1, 0, 0, 45758), (0, 1, -1, 0, -40923), (1, 0, 0, 0, -34720),
    (0, 1, 1, 0, -30383), (2, 0, 0, -2, 15327), (0, 0, 1, 2, -12528), (0, 0, 1, -2, 10980),
    (4, 0, -1, 0, 10675), (0, 0, 3, 0, 10034), (4, 0, -2, 0, 8548), (2, 1, -1, 0, -7888),
    (2, 1, 0, 0, -6766), (1, 0, -1, 0, -5163), (1, 1, 0, 0, 4987), (2, -1, 1, 0, 4036),
    (2, 0, 2, 0, 3994), (4, 0, 0, 0, 3861), (2, 0, -3, 0, 3665), (0, 1, -2, 0, -2689),
    (2, 0, -1, 2, -2602), (2, -1, -2, 0, 2390), (1, 0, 1, 0, -2348), (2, -2, 0, 0, 2236),
)


def moon_longitude(t: datetime) -> float:
    """Geocentric ecliptic longitude of the Moon, degrees (main terms of Meeus ch. 47;
    accurate to a few hundredths of a degree, plenty for phase and illumination)."""
    T = _centuries(t)
    Lp, D, M, Mp, F, E = _moon_arguments(T)
    total = 0.0
    for d, m, mp, f, coeff in _MOON_LON_TERMS:
        term = coeff * _sin(d * D + m * M + mp * Mp + f * F)
        total += term * (E ** abs(m))
    A1 = 119.75 + 131.849 * T
    A2 = 53.09 + 479264.290 * T
    total += 3958 * _sin(A1) + 1962 * _sin(Lp - F) + 318 * _sin(A2)
    return (Lp + total / 1e6) % 360


def elongation(t: datetime) -> float:
    """Moon's longitude minus the Sun's, 0..360 degrees. 0 = new, 180 = full."""
    return (moon_longitude(t) - sun_longitude(t)) % 360


def phase_angle(t: datetime) -> float:
    """Selenocentric Sun-Moon-Earth angle i, degrees (Meeus eq. 48.4). 0 = full, 180 = new."""
    _, D, M, Mp, _, _ = _moon_arguments(_centuries(t))
    i = (180 - D % 360 - 6.289 * _sin(Mp) + 2.100 * _sin(M) - 1.274 * _sin(2 * D - Mp)
         - 0.658 * _sin(2 * D) - 0.214 * _sin(2 * Mp) - 0.110 * _sin(D))
    return abs((i + 180) % 360 - 180)


def illuminated_fraction(t: datetime) -> float:
    return (1 + _cos(phase_angle(t))) / 2


def intermediate_name(elong: float) -> PhaseName:
    """Crescent/gibbous name from the elongation (used on days without a principal phase)."""
    e = elong % 360
    if e < 90:
        return "Waxing Crescent"
    if e < 180:
        return "Waxing Gibbous"
    if e < 270:
        return "Waning Gibbous"
    return "Waning Crescent"


def lit_side(waxing: bool, latitude: float) -> Literal["right", "left"]:
    """Which limb is lit as seen by an observer: waxing is on the right in the
    northern hemisphere, mirrored in the southern."""
    north = latitude >= 0
    return "right" if waxing == north else "left"


# ---------- payload ----------

class MoonPhaseEvent(BaseModel):
    name: PhaseName
    at: datetime


class MoonInfo(BaseModel):
    phase_name: PhaseName
    illumination: float           # 0..1, illuminated fraction of the disc
    illumination_pct: int
    phase_angle_deg: float        # 0 = full, 180 = new
    elongation_deg: float         # 0..360; < 180 waxing
    waxing: bool
    age_days: float               # since the previous new moon
    lit_side: Literal["right", "left"]
    hemisphere: Literal["north", "south"]
    previous_new_moon: datetime
    next_phases: list[MoonPhaseEvent]   # the next four principal phases


class SunInfo(BaseModel):
    sunrise: datetime
    sunset: datetime
    daylight_minutes: int
    status: Literal["before_sunrise", "day", "after_sunset"]
    progress: float               # 0 at sunrise .. 1 at sunset (clamped outside daytime)
    next_sunrise: datetime | None = None   # tomorrow's, when the provider has it
    source: str


class Astronomy(BaseModel):
    computed_at: datetime
    date: date                    # the local calendar day the phase name refers to
    timezone: str
    latitude: float
    moon: MoonInfo
    sun: SunInfo | None = None    # None when the weather provider has no sunrise/sunset
    method: str = "Meeus, Astronomical Algorithms (ch. 25, 47-49); computed on the Pi"


def moon_info(now: datetime, tz: ZoneInfo, latitude: float) -> MoonInfo:
    local = now.astimezone(tz)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    elong = elongation(now)
    waxing = elong < 180
    name = intermediate_name(elong)
    for phase, at in phases_between(day_start.astimezone(timezone.utc), day_end.astimezone(timezone.utc)):
        name = phase
    frac = illuminated_fraction(now)
    prev_new = previous_new_moon(now)
    upcoming = phases_between(now, now + timedelta(days=SYNODIC_MONTH + 2))[:4]
    return MoonInfo(
        phase_name=name,
        illumination=round(frac, 4),
        illumination_pct=round(frac * 100),
        phase_angle_deg=round(phase_angle(now), 2),
        elongation_deg=round(elong, 2) % 360,
        waxing=waxing,
        age_days=round((now - prev_new).total_seconds() / 86400, 1),
        lit_side=lit_side(waxing, latitude),
        hemisphere="north" if latitude >= 0 else "south",
        previous_new_moon=prev_new,
        next_phases=[MoonPhaseEvent(name=n, at=t.astimezone(tz)) for n, t in upcoming],
    )


def sun_info(now: datetime, sunrise: datetime | None, sunset: datetime | None,
             next_sunrise: datetime | None = None, source: str = "") -> SunInfo | None:
    """Day length, status and arc position from the provider's sunrise/sunset."""
    if sunrise is None or sunset is None or sunset <= sunrise:
        return None
    span = (sunset - sunrise).total_seconds()
    if now < sunrise:
        status, progress = "before_sunrise", 0.0
    elif now >= sunset:
        status, progress = "after_sunset", 1.0
    else:
        status, progress = "day", (now - sunrise).total_seconds() / span
    return SunInfo(
        sunrise=sunrise, sunset=sunset, daylight_minutes=round(span / 60), status=status,
        progress=round(progress, 4),
        next_sunrise=next_sunrise, source=source,
    )


def astronomy_for(now: datetime, tz_name: str, latitude: float, snapshot=None) -> Astronomy:
    """Everything the weather card shows about the Sun and Moon at ``now``.
    ``snapshot`` is the (trimmed) WeatherSnapshot, or None when weather is unavailable."""
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    sun = None
    if snapshot is not None:
        today = local.date()
        rise, set_ = snapshot.sunrise, snapshot.sunset
        if not (rise and set_ and rise.astimezone(tz).date() == today):
            # Only today's times describe today; never reuse another day's.
            rise = set_ = None
            for d in snapshot.daily or []:
                if d.date == today:
                    rise, set_ = d.sunrise, d.sunset
        next_rise = next((d.sunrise for d in snapshot.daily or []
                          if d.date > today and d.sunrise is not None), None)
        sun = sun_info(now, rise, set_, next_rise, source=snapshot.source)
    return Astronomy(
        computed_at=local, date=local.date(), timezone=tz_name, latitude=latitude,
        moon=moon_info(now, tz, latitude), sun=sun,
    )
