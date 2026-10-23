"""Meal plan: monthly calendar rule, data integrity, scaling, prep tasks, records."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.nutrition import (NutritionLog, check_plan, day_view, load_plan, local_today, plan_day, plan_week,
                           prep_tasks, scale_text, week_key)


@pytest.mark.parametrize("d, day", [
    (date(2026, 11, 1), 1), (date(2026, 11, 15), 15), (date(2026, 11, 30), 30), (date(2026, 12, 1), 1),
    (date(2026, 12, 31), 30), (date(2027, 1, 1), 1), (date(2027, 2, 28), 28), (date(2027, 3, 1), 1),
    (date(2028, 2, 29), 29), (date(2028, 3, 1), 1), (date(2026, 12, 30), 30),
])
def test_plan_day_follows_the_calendar(d, day):
    assert plan_day(d) == day


def test_every_day_of_several_years():
    d = date(2026, 1, 1)
    while d < date(2029, 1, 1):
        assert plan_day(d) == min(d.day, 30)
        nxt = d + timedelta(days=1)
        if nxt.day == 1:
            assert plan_day(nxt) == 1                      # restarts every month
        d = nxt


def test_time_zone_decides_the_date():
    utc_evening = datetime(2026, 10, 31, 19, 0, tzinfo=UTC)   # 00:30 on 1 Nov in India
    assert local_today("Asia/Kolkata", utc_evening) == date(2026, 11, 1)
    assert local_today("UTC", utc_evening) == date(2026, 10, 31)


def test_weeks_and_keys():
    assert [plan_week(d) for d in (1, 7, 8, 14, 15, 21, 22, 30)] == [1, 1, 2, 2, 3, 3, 4, 4]
    assert week_key(date(2026, 12, 31)) == "2026-12-w4" and week_key(date(2027, 1, 1)) == "2027-01-w1"


def test_plan_data_is_complete_and_consistent():
    plan = load_plan()
    assert check_plan(plan) == []
    assert len(plan["recipes"]) == 36 and len(plan["schedule"]) == 30
    assert plan["schedule"][11]["dinner"]["code"] == "D10"            # Day 12 dinner
    broken = {**plan, "schedule": [{**plan["schedule"][0], "lunch": {"label": "x", "code": "L99"}}] + plan["schedule"][1:]}
    assert any("L99" in p for p in check_plan(broken))


def test_scaling_keeps_times_and_units():
    assert scale_text("yellow moong dal 1 cup (soaked 3 h)", 1.5) == "yellow moong dal 1½ cup (soaked 3 h)"
    assert scale_text("4 cups water (3–4 whistles)", 0.5) == "2 cups water (3–4 whistles)"
    assert scale_text("paneer 200 g (crumbled)", 1.5) == "paneer 300 g (crumbled)"


def test_tomorrow_prep_comes_from_the_recipes():
    plan = load_plan()
    tasks = prep_tasks(plan, date(2026, 11, 1))                       # Day 1: B1, L1, D2
    texts = " ".join(t["text"] for t in tasks)
    assert "Soak 1 cup dry rajma overnight" in texts and "3 h" in texts
    assert all(t["when"] in ("tonight", "tomorrow") for t in tasks)


def test_records_are_per_calendar_date(tmp_path):
    plan = load_plan()
    log = NutritionLog(tmp_path / "aura.db")
    log.set_meal(date(2026, 10, 1), "breakfast", "person1", True, 1, "B1")
    assert log.meals(date(2026, 11, 1))["breakfast"]["person1"] is False     # Day 1 of the next month is separate
    assert log.meals(date(2026, 10, 1))["breakfast"]["person1"] is True
    v = day_view(plan, log, date(2026, 10, 31))
    assert v["plan_day"] == 30 and v["tomorrow"]["plan_day"] == 1 and v["tomorrow"]["month_restart"]
