"""Meal plan API: calendar mapping through the real endpoints, records per date, prep, groceries."""

import pytest

from app.nutrition import load_plan
from conftest import make_client


def lan(tmp_path, **kw):
    return make_client(tmp_path, edit_access="lan", **kw)


@pytest.mark.parametrize("d, day, tomorrow_day", [
    ("2026-11-01", 1, 2), ("2026-11-30", 30, 1), ("2026-12-31", 30, 1), ("2027-02-28", 28, 1),
    ("2028-02-28", 28, 29), ("2028-02-29", 29, 1), ("2026-12-30", 30, 30),
])
def test_day_endpoint_follows_the_calendar(tmp_path, d, day, tomorrow_day):
    body = lan(tmp_path).get(f"/api/meals/day?date={d}").json()
    assert body["plan_day"] == day and body["tomorrow"]["plan_day"] == tomorrow_day
    plan = load_plan()
    assert [m["code"] for m in body["meals"]] == [plan["schedule"][day - 1][m]["code"] for m in ("breakfast", "lunch", "dinner")]


def test_today_uses_home_time_zone(tmp_path):
    body = lan(tmp_path).get("/api/meals/day").json()
    assert body["date"] == body["today"] and body["timezone"] == "Asia/Kolkata"


def test_day_shows_totals_portions_and_names(tmp_path):
    c = lan(tmp_path)
    c.patch("/api/settings/meals", json={"person1_name": "Arpit", "person2_name": "Riya"})
    body = c.get("/api/meals/day?date=2026-11-12").json()              # Day 12
    assert body["totals"] == {"kcal": {"person1": 2305, "person2": 1590}, "protein_g": {"person1": 185, "person2": 131}}
    dinner = body["meals"][2]
    assert dinner["code"] == "D10" and dinner["paneer"] and dinner["person1_extra"]["kcal"] == 190
    assert body["people"]["person1"] == "Arpit" and body["people"]["person2"] == "Riya"


def test_meal_records_are_per_calendar_date(tmp_path):
    c = lan(tmp_path)
    r = c.post("/api/meals/eaten", json={"date": "2026-10-01", "meal": "breakfast", "person": "person2", "done": True})
    assert r.status_code == 200 and r.json()["eaten"]["breakfast"]["person2"] is True
    assert c.get("/api/meals/day?date=2026-11-01").json()["eaten"]["breakfast"]["person2"] is False   # also Day 1
    assert c.get("/api/meals/day?date=2026-10-01").json()["eaten"]["breakfast"]["person2"] is True
    hist = c.app.state.meal_log.history(__import__("datetime").date(2026, 10, 1), __import__("datetime").date(2026, 10, 31))
    assert hist == [{"date": "2026-10-01", "meal": "breakfast", "person": "person2", "done": True, "plan_day": 1, "recipe": "B1"}]


def test_prepare_for_tomorrow_and_persistence(tmp_path):
    c = lan(tmp_path)
    day = c.get("/api/meals/day?date=2026-11-08").json()               # tomorrow = Day 9: B7 overnight oats
    tasks = {t["id"]: t for t in day["tomorrow"]["tasks"]}
    oats = tasks["B7:overnight-oats"]
    assert oats["when"] == "tonight" and "refrigerate overnight" in oats["text"] and not oats["done"]
    assert tasks["B7:overnight-oats-top"]["when"] == "tomorrow"
    assert c.post("/api/meals/prep", json={"date": "2026-11-09", "task": "B7:overnight-oats", "done": True}).status_code == 200
    again = {t["id"]: t for t in c.get("/api/meals/day?date=2026-11-08").json()["tomorrow"]["tasks"]}
    assert again["B7:overnight-oats"]["done"] is True
    # next month, same plan day: a fresh task list, history kept
    other = {t["id"]: t for t in c.get("/api/meals/day?date=2026-12-08").json()["tomorrow"]["tasks"]}
    assert other["B7:overnight-oats"]["done"] is False
    assert c.post("/api/meals/prep", json={"date": "2026-11-09", "task": "L1:soak-rajma", "done": True}).status_code == 422
    assert c.post("/api/meals/prep", json={"date": "2026-11-09", "task": "bad task!", "done": True}).status_code == 422


def test_unstated_soak_time_is_flagged_not_invented(tmp_path):
    day = lan(tmp_path).get("/api/meals/day?date=2026-11-02").json()  # tomorrow Day 3: L2 chole
    soak = next(t for t in day["tomorrow"]["tasks"] if t["id"] == "L2:soak-chole")
    assert soak["unstated"] and "doesn't state how long" in soak["text"]


def test_recipe_details_and_scaling(tmp_path):
    c = lan(tmp_path)
    r = c.get("/api/meals/recipe/L1").json()
    assert r["name"] == "Rajma + Brown Rice + Salad" and r["base_serving"]["kcal"] == 565
    assert r["used_on_days"] == [1, 15, 27] and r["method"][0].startswith("Pressure-cook soaked rajma")
    assert r["person1_extra"] == {"text": "+½ cup extra rice + 150 g extra curd", "kcal": 250, "protein_g": 12}
    big = c.get("/api/meals/recipe/L1?servings=4").json()
    rajma = big["scaled_ingredients"][0]
    assert rajma["text"] == "rajma 1 cup dry (soaked overnight)" and rajma["scaled"] == "rajma 2 cup dry (soaked overnight)"
    assert big["base_serving"] == r["base_serving"]                       # per-serving nutrition unchanged
    hindi = c.get("/api/meals/recipe/L4").json()["notes"]
    assert hindi == [{"text": "Soya ko dabakar paani nikaalo, warna kaccha lagta hai.", "lang": "hi"}]
    assert c.get("/api/meals/recipe/X9").status_code == 404
    assert c.get("/api/meals/recipe/L1?servings=0").status_code == 422


def test_every_scheduled_recipe_is_reachable(tmp_path):
    c = lan(tmp_path)
    codes = {r["code"] for r in c.get("/api/meals/recipes").json()}
    assert len(codes) == 36
    for d in load_plan()["schedule"]:
        for m in ("breakfast", "lunch", "dinner"):
            assert d[m]["code"] in codes


def test_weekly_groceries_and_idempotent_adding(tmp_path):
    c = lan(tmp_path)
    g = c.get("/api/meals/groceries?date=2026-11-10").json()           # Day 10 -> week 2
    assert g["week"] == 2 and g["week_key"] == "2026-11-w2"
    pulses = g["categories"][0]["items"]
    lobia = next(i for i in pulses if i["name"].startswith("Lobia"))
    assert lobia["quantity"] == "250 g"
    assert c.post("/api/meals/groceries/check", json={"week": "2026-11-w2", "item": lobia["id"], "purchased": True}).status_code == 200
    assert next(i for i in c.get("/api/meals/groceries?date=2026-11-10").json()["categories"][0]["items"]
                if i["id"] == lobia["id"])["purchased"] is True
    assert next(i for i in c.get("/api/meals/groceries?date=2026-12-10").json()["categories"][0]["items"]
                if i["id"] == lobia["id"])["purchased"] is False                       # December's week 2 is separate
    first = c.post("/api/meals/groceries/add", json={"items": [{"name": "Lobia", "quantity": "250 g"}]}).json()
    second = c.post("/api/meals/groceries/add", json={"items": [{"name": "lobia", "quantity": "250 g"}]}).json()
    assert first == {"added": ["Lobia"], "skipped": []} and second == {"added": [], "skipped": ["Lobia"]}
    assert sum(1 for i in c.get("/api/groceries").json()["items"] if i["name"].lower() == "lobia") == 1
    assert next(i for i in c.get("/api/meals/groceries?date=2026-11-10").json()["categories"][0]["items"]
                if i["id"] == lobia["id"])["on_list"] is True


def test_tomorrow_ingredients_combine_only_matching_units(tmp_path):
    g = lan(tmp_path).get("/api/meals/groceries?date=2026-10-31").json()  # tomorrow = Day 1: B1, L1, D2
    items = {i["name"]: i for i in g["tomorrow"]["items"]}
    assert g["tomorrow"]["recipes"] == ["B1", "L1", "D2"]
    assert items["Curd"]["quantity"] == "450 g" and items["Curd"]["recipes"] == ["B1", "L1"]
    assert items["Rajma"]["quantity"] == "1 cup"
    assert items["Salt"]["staple"] is True


def test_turning_the_plan_off(tmp_path):
    c = lan(tmp_path)
    c.patch("/api/settings/meals", json={"enabled": False})
    assert c.get("/api/meals/day").status_code == 404
    assert c.get("/api/config").json()["meals_enabled"] is False


def test_changes_need_edit_access(tmp_path):
    c = make_client(tmp_path, client_ip="192.168.1.50")                # pin mode, not unlocked
    assert c.get("/api/meals/day").status_code == 200
    assert c.post("/api/meals/eaten", json={"date": "2026-10-01", "meal": "lunch", "person": "person1", "done": True}).status_code == 401
    assert c.post("/api/meals/groceries/add", json={"items": [{"name": "Rajma"}]}).status_code == 401


def test_notes(tmp_path):
    c = lan(tmp_path)
    assert c.post("/api/meals/note", json={"date": "2026-11-03", "text": "Used toor dal instead"}).json()["note"] == "Used toor dal instead"
    assert c.get("/api/meals/day?date=2026-11-03").json()["note"] == "Used toor dal instead"
    assert c.post("/api/meals/note", json={"date": "2026-11-03", "text": "x" * 501}).status_code == 422


def test_rest_of_month_endpoint_runs_once(tmp_path):
    c = lan(tmp_path)
    g = c.get("/api/meals/groceries").json()
    assert g["auto"]["enabled"] is True and g["auto"]["month_done"] is False
    r = c.post("/api/meals/groceries/rest-of-month")
    assert r.status_code == 200 and r.json()["added"]
    items = c.get("/api/groceries").json()["items"]
    assert all(i["notes"].startswith("Meal plan") for i in items)
    assert c.post("/api/meals/groceries/rest-of-month").status_code == 409
    after = c.get("/api/meals/groceries").json()["auto"]
    assert after["month_done"] is True and any(b["batch"].endswith("-rest") for b in after["batches"])
    assert make_client(tmp_path / "x", client_ip="192.168.1.50").post("/api/meals/groceries/rest-of-month").status_code == 401


def test_note_tells_other_screens(tmp_path):
    c = lan(tmp_path)
    q = c.app.state.broker.subscribe()
    c.post("/api/meals/note", json={"date": "2026-11-03", "text": "Less salt"})
    assert q.get_nowait() == ("meals", {"date": "2026-11-03"})


def test_add_skips_names_already_on_the_list_even_from_the_automatic_batch(tmp_path):
    c = lan(tmp_path)
    c.post("/api/meals/groceries/rest-of-month")
    r = c.post("/api/meals/groceries/add", json={"items": [{"name": "Paneer (fresh)"}, {"name": "Saffron"}]}).json()
    assert r == {"added": ["Saffron"], "skipped": ["Paneer (fresh)"]}
