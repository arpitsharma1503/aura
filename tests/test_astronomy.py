"""Sun & Moon: lunar phase maths (Meeus) and the Sun's arc from provider times."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app import astronomy as A
from app.weather import DailyForecast

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
AGRA = 27.18
SYDNEY = -33.87


def at(s: str, tz=IST) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=tz)


# ---------- phase instants ----------

def test_meeus_worked_examples():
    # Astronomical Algorithms, example 49.a: new moon of 1977 Feb 18, JDE 2443192.65118.
    assert A.phase_jde(-283) == pytest.approx(2443192.65118, abs=2e-5)
    # Example 49.b: last quarter of 2044 January, JDE 2467636.49186.
    assert A.phase_jde(544.75) == pytest.approx(2467636.49186, abs=2e-5)


@pytest.mark.parametrize("name, published", [
    # Published UTC times (timeanddate / theskylive / USNO, rounded to the minute).
    ("New Moon", "2026-10-10T15:50"),
    ("Full Moon", "2026-10-26T04:12"),
    ("New Moon", "2026-11-09T07:02"),
    ("New Moon", "2026-08-12T17:36"),   # total solar eclipse
    ("Full Moon", "2026-03-03T11:38"),  # total lunar eclipse
])
def test_phase_instants_match_published_tables(name, published):
    target = at(published, UTC)
    found = [t for n, t in A.phases_between(target - timedelta(days=1), target + timedelta(days=1)) if n == name]
    assert len(found) == 1
    assert abs((found[0] - target).total_seconds()) < 180


def test_phases_cycle_in_order():
    names = [n for n, _ in A.phases_between(at("2026-01-01T00:00", UTC), at("2027-01-01T00:00", UTC))]
    cycle = ["New Moon", "First Quarter", "Full Moon", "Last Quarter"]
    start = cycle.index(names[0])
    assert names == [cycle[(start + i) % 4] for i in range(len(names))]
    # 2026 has 13 full moons (the second in May is a "blue moon") and 12 new moons.
    assert names.count("Full Moon") == 13 and names.count("New Moon") == 12


# ---------- all eight phase names ----------

@pytest.mark.parametrize("when, name, waxing, lo, hi", [
    ("2026-10-10T20:00", "New Moon", None, 0, 1),           # day of the 15:50 UTC new moon
    ("2026-10-14T20:00", "Waxing Crescent", True, 8, 25),
    ("2026-10-18T20:00", "First Quarter", True, 45, 55),
    ("2026-10-22T20:00", "Waxing Gibbous", True, 75, 95),
    ("2026-10-26T20:00", "Full Moon", False, 98, 100),
    ("2026-10-29T20:00", "Waning Gibbous", False, 75, 95),
    ("2026-11-02T06:00", "Last Quarter", False, 42, 58),
    ("2026-11-05T06:00", "Waning Crescent", False, 8, 30),
])
def test_eight_phases(when, name, waxing, lo, hi):
    m = A.moon_info(at(when), IST, AGRA)
    assert m.phase_name == name
    assert lo <= m.illumination_pct <= hi
    if waxing is not None:
        assert m.waxing is waxing


def test_intermediate_names_by_elongation():
    assert A.intermediate_name(10) == "Waxing Crescent"
    assert A.intermediate_name(100) == "Waxing Gibbous"
    assert A.intermediate_name(200) == "Waning Gibbous"
    assert A.intermediate_name(300) == "Waning Crescent"
    assert A.intermediate_name(370) == "Waxing Crescent"


def test_principal_phase_named_for_the_whole_local_day():
    # Full moon 2026-10-26 04:12 UTC = 09:42 IST: "Full Moon" all day in India,
    # including before the instant (still technically waxing).
    morning = A.moon_info(at("2026-10-26T00:30"), IST, AGRA)
    night = A.moon_info(at("2026-10-26T23:30"), IST, AGRA)
    assert morning.phase_name == night.phase_name == "Full Moon"
    assert morning.waxing and not night.waxing
    assert A.moon_info(at("2026-10-27T00:30"), IST, AGRA).phase_name == "Waning Gibbous"


# ---------- illumination and age ----------

def test_illumination_extremes_and_quarters():
    assert A.illuminated_fraction(at("2026-10-26T04:12", UTC)) > 0.995
    assert A.illuminated_fraction(at("2026-10-10T15:50", UTC)) < 0.005
    fq = [t for n, t in A.phases_between(at("2026-10-17T00:00", UTC), at("2026-10-20T00:00", UTC))][0]
    assert A.illuminated_fraction(fq) == pytest.approx(0.5, abs=0.02)


def test_illumination_matches_elongation():
    # k = (1 + cos i)/2 with i ≈ 180° - elongation (to within the Moon's ~6° wobble).
    t = at("2026-10-14T20:00")
    e = A.elongation(t)
    assert A.illuminated_fraction(t) == pytest.approx((1 - A._cos(e)) / 2, abs=0.04)


def test_age_since_previous_new_moon():
    m = A.moon_info(at("2026-10-24T22:00"), IST, AGRA)
    expected = (at("2026-10-24T22:00") - at("2026-10-10T15:50", UTC)).total_seconds() / 86400
    assert m.age_days == pytest.approx(expected, abs=0.05)
    assert m.previous_new_moon.date() == date(2026, 10, 10)
    assert 0 <= A.moon_info(at("2026-10-11T00:00"), IST, AGRA).age_days < 1


def test_next_phases_listed_in_order():
    m = A.moon_info(at("2026-10-24T22:00"), IST, AGRA)
    assert [e.name for e in m.next_phases] == ["Full Moon", "Last Quarter", "New Moon", "First Quarter"]
    assert m.next_phases[0].at.date() == date(2026, 10, 26)
    assert all(a.at < b.at for a, b in zip(m.next_phases, m.next_phases[1:]))


# ---------- orientation ----------

def test_waxing_lit_on_right_in_northern_hemisphere():
    assert A.moon_info(at("2026-10-14T20:00"), IST, AGRA).lit_side == "right"
    assert A.moon_info(at("2026-11-05T06:00"), IST, AGRA).lit_side == "left"


def test_southern_hemisphere_is_mirrored():
    syd = ZoneInfo("Australia/Sydney")
    waxing = A.moon_info(at("2026-10-14T20:00"), syd, SYDNEY)
    waning = A.moon_info(at("2026-11-05T06:00"), syd, SYDNEY)
    assert waxing.waxing and waxing.lit_side == "left" and waxing.hemisphere == "south"
    assert not waning.waxing and waning.lit_side == "right"


@pytest.mark.parametrize("waxing, lat, side", [
    (True, 27, "right"), (False, 27, "left"), (True, -27, "left"), (False, -27, "right"), (True, 0, "right"),
])
def test_lit_side_rule(waxing, lat, side):
    assert A.lit_side(waxing, lat) == side


# ---------- the Sun ----------

RISE, SET = at("2026-10-24T06:22"), at("2026-10-24T17:44")


def test_sun_before_sunrise():
    s = A.sun_info(at("2026-10-24T05:00"), RISE, SET)
    assert s.status == "before_sunrise" and s.progress == 0
    assert s.daylight_minutes == 11 * 60 + 22


def test_sun_during_the_day():
    mid = RISE + (SET - RISE) / 2
    s = A.sun_info(mid, RISE, SET)
    assert s.status == "day" and s.progress == pytest.approx(0.5, abs=1e-3)
    s = A.sun_info(RISE + (SET - RISE) / 4, RISE, SET)
    assert s.progress == pytest.approx(0.25, abs=1e-3)
    assert A.sun_info(RISE, RISE, SET).status == "day"


def test_sun_after_sunset_offers_tomorrows_sunrise():
    tomorrow = at("2026-10-25T06:23")
    s = A.sun_info(at("2026-10-24T20:00"), RISE, SET, next_sunrise=tomorrow)
    assert s.status == "after_sunset" and s.progress == 1 and s.next_sunrise == tomorrow
    assert A.sun_info(at("2026-10-24T12:00"), RISE, SET, next_sunrise=tomorrow).next_sunrise == tomorrow


def test_sun_missing_or_bad_data_is_none():
    assert A.sun_info(at("2026-10-24T12:00"), None, SET) is None
    assert A.sun_info(at("2026-10-24T12:00"), SET, RISE) is None


def _snapshot(sunrise, sunset, daily):
    return SimpleNamespace(sunrise=sunrise, sunset=sunset, daily=daily, source="Open-Meteo")


def _day(d: date, rise: str, set_: str) -> DailyForecast:
    return DailyForecast(date=d, condition="clear", description="Clear", high_c=30, low_c=20,
                         sunrise=at(f"{d}T{rise}"), sunset=at(f"{d}T{set_}"))


def test_astronomy_uses_todays_provider_times_only():
    today, tomorrow = date(2026, 10, 24), date(2026, 10, 25)
    daily = [_day(today, "06:22", "17:44"), _day(tomorrow, "06:23", "17:43")]
    a = A.astronomy_for(at("2026-10-24T21:00"), "Asia/Kolkata", AGRA, _snapshot(RISE, SET, daily))
    assert a.sun.status == "after_sunset" and a.sun.next_sunrise == at("2026-10-25T06:23")
    assert a.date == today and a.moon.hemisphere == "north"
    # Yesterday's cached sunrise/sunset is never presented as today's.
    stale = A.astronomy_for(at("2026-10-25T03:00"), "Asia/Kolkata", AGRA,
                            _snapshot(RISE, SET, [_day(today, "06:22", "17:44")]))
    assert stale.sun is None and stale.moon is not None
    # ...but today's daily entry is used when the snapshot's top-level times are old.
    fixed = A.astronomy_for(at("2026-10-25T03:00"), "Asia/Kolkata", AGRA, _snapshot(RISE, SET, daily))
    assert fixed.sun.sunrise == at("2026-10-25T06:23") and fixed.sun.status == "before_sunrise"


def test_astronomy_without_weather_has_moon_only():
    a = A.astronomy_for(at("2026-10-24T21:00"), "Asia/Kolkata", AGRA, None)
    assert a.sun is None and a.moon.phase_name == "Waxing Gibbous"


def test_date_change_at_local_midnight():
    before = A.astronomy_for(at("2026-10-25T23:59"), "Asia/Kolkata", AGRA)
    after = A.astronomy_for(at("2026-10-26T00:01"), "Asia/Kolkata", AGRA)
    assert before.date == date(2026, 10, 25) and after.date == date(2026, 10, 26)
    assert before.moon.phase_name == "Waxing Gibbous" and after.moon.phase_name == "Full Moon"


def test_naive_datetimes_rejected():
    with pytest.raises(ValueError):
        A.julian_day(datetime(2026, 10, 24))
