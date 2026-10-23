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


# ----- automatic plan groceries -----

from app.groceries import GroceryStore, ItemIn  # noqa: E402
from app.nutrition import PlanGroceries, grocery_frequency, plan_grocery_batch  # noqa: E402


@pytest.fixture
def pg(tmp_path):
    store = GroceryStore(tmp_path / "aura.db")
    p = PlanGroceries(tmp_path / "aura.db", store)
    yield p
    p.close()


def by_name(items):
    return {i["name"]: i for i in items}


@pytest.mark.parametrize("cat, name, freq", [
    ("Pulses / legumes", "Rajma", "monthly"), ("Grains", "Atta", "monthly"), ("Soya / tofu / paneer", "Paneer", "weekly"),
    ("Soya / tofu / paneer", "Firm tofu", "weekly"), ("Soya / tofu / paneer", "Soya chunks", "monthly"),
    ("Vegetables", "Spinach", "weekly"), ("Dairy", "Milk", "weekly"), ("Grains", "Idli/dosa batter", "weekly"),
    ("Optional supplement", "Whey", None),
])
def test_grocery_frequency(cat, name, freq):
    assert grocery_frequency(cat, name) == freq


def test_monthly_batch_sums_dry_goods_over_the_four_weeks():
    plan = load_plan()
    month = by_name(plan_grocery_batch(plan, "monthly", [1, 2, 3, 4]))
    assert month["Atta"]["quantity"] == "8 kg" and month["Moong dal"]["quantity"] == "1.7 kg"
    assert all(i["category"] == "pantry" for i in month.values())
    assert not any(n.lower().startswith(("paneer", "firm tofu", "milk", "whey")) for n in month)
    week = by_name(plan_grocery_batch(plan, "weekly", [4]))
    assert week["Paneer"]["quantity"] == "400 g" and week["Firm tofu"]["quantity"] == "700 g"
    assert not any(n in week for n in ("Atta", "Rajma", "Moong dal"))


def test_rest_of_month_then_auto_never_duplicates(pg):
    plan = load_plan()
    first = pg.rest_of_month(plan, date(2026, 10, 23))                   # Day 23 -> week 4
    assert first["added"] and not first["updated"]
    names = [i.name for i in pg.store.load().items]
    assert len(names) == len({n.lower() for n in names})
    assert pg.auto(plan, date(2026, 10, 24)) == []                       # October is covered
    assert pg.auto(plan, date(2026, 10, 31)) == []
    assert pg.rest_of_month(plan, date(2026, 10, 23))["added"] == []     # running it again adds nothing
    nov = pg.auto(plan, date(2026, 11, 1))
    assert [r["batch"] for r in nov] == ["2026-11-month", "2026-11-w1-fresh"]
    assert pg.auto(plan, date(2026, 11, 1)) == [] and pg.auto(plan, date(2026, 11, 7)) == []
    assert [r["batch"] for r in pg.auto(plan, date(2026, 11, 8))] == ["2026-11-w2-fresh"]
    assert [r["batch"] for r in pg.auto(plan, date(2026, 11, 30))] == ["2026-11-w4-fresh"]   # week 3 missed while off: not backfilled


def test_households_own_items_are_left_alone_and_plan_items_updated(pg):
    plan = load_plan()
    pg.store.add(ItemIn(name="Paneer", quantity="1 kg", notes="for guests"))
    r = pg.auto(plan, date(2026, 11, 1))
    fresh = r[1]
    assert "Paneer" in fresh["skipped"]
    paneer = next(i for i in pg.store.load().items if i.name == "Paneer")
    assert paneer.quantity == "1 kg" and paneer.notes == "for guests"
    # an unbought plan item from an earlier week gets the new week's amount instead of a second entry
    atta_before = [i for i in pg.store.load().items if i.name == "Atta"]
    pg.auto(plan, date(2026, 12, 1))
    atta_after = [i for i in pg.store.load().items if i.name == "Atta"]
    assert len(atta_before) == len(atta_after) == 1


def test_auto_starts_next_month_unless_today_is_the_first(tmp_path):
    plan = load_plan()
    store = GroceryStore(tmp_path / "a.db")
    p = PlanGroceries(tmp_path / "a.db", store)
    assert p.auto(plan, date(2026, 10, 23)) == [] and p.meta("auto_from") == "2026-11"
    assert store.load().items == []
    q = PlanGroceries(tmp_path / "b.db", GroceryStore(tmp_path / "b.db"))
    assert [r["batch"] for r in q.auto(plan, date(2026, 12, 1))] == ["2026-12-month", "2026-12-w1-fresh"]
    assert q.meta("auto_from") == "2026-12"
    p.close(); q.close()
