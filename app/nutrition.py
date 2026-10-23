"""Monthly meal plan ("1-Month Body Recomposition Plan"): which plan day it is,
today's and tomorrow's meals, recipes, next-day preparation, weekly groceries,
and the household's daily records.

Calendar rule: the plan restarts on the 1st of every month. Day N of the month
is plan day N; days 30 and 31 both use plan day 30 (the last one). Dates are
taken in the home time zone, so the day changes at local midnight.

The plan itself is read-only data (app/data/meal_plan.json, built and validated
from the PDF by tools/extract_meal_plan.py). What people do with it - meals
eaten, preparation done, groceries bought, notes - is stored per real calendar
date in SQLite, so "Day 1" in March and "Day 1" in April are separate records
and nothing is lost when a new month starts.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from .storage import make_private

PLAN_FILE = Path(__file__).resolve().parent / "data" / "meal_plan.json"
MEALS = ("breakfast", "lunch", "dinner")
PEOPLE = ("person1", "person2")
LAST_PLAN_DAY = 30
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# --------------------------------------------------------------------------- calendar

def plan_day(d: date) -> int:
    """1st of the month = Day 1 ... the 30th and 31st = Day 30. Restarts every month."""
    return min(d.day, LAST_PLAN_DAY)


def plan_week(day: int) -> int:
    return 1 if day <= 7 else 2 if day <= 14 else 3 if day <= 21 else 4


def local_today(tz: str, now: datetime | None = None) -> date:
    now = now or datetime.now(UTC)
    return now.astimezone(ZoneInfo(tz)).date()


def week_key(d: date) -> str:
    """Grocery checklists belong to a plan week of a real month: '2026-11-w2'."""
    return f"{d:%Y-%m}-w{plan_week(plan_day(d))}"


# --------------------------------------------------------------------------- plan data

@lru_cache(maxsize=1)
def load_plan(path: Path = PLAN_FILE) -> dict:
    plan = json.loads(path.read_text())
    problems = check_plan(plan)
    if problems:
        raise ValueError("meal plan data is inconsistent: " + "; ".join(problems))
    return plan


def check_plan(plan: dict) -> list[str]:
    """Every schedule entry must point at a recipe of the same meal type."""
    out = []
    recipes = plan.get("recipes", {})
    days = plan.get("schedule", [])
    if [d["day"] for d in days] != list(range(1, LAST_PLAN_DAY + 1)):
        out.append("schedule must cover days 1-30")
    for d in days:
        for meal in MEALS:
            code = d[meal]["code"]
            if code not in recipes:
                out.append(f"day {d['day']} {meal} refers to missing recipe {code}")
            elif recipes[code]["meal"] != meal:
                out.append(f"day {d['day']} {meal}: {code} is a {recipes[code]['meal']} recipe")
    for code in plan.get("prep", {}):
        if code not in recipes:
            out.append(f"prep rules for unknown recipe {code}")
    return out


def day_entry(plan: dict, d: date) -> dict:
    return plan["schedule"][plan_day(d) - 1]


def used_on_days(plan: dict, code: str) -> list[int]:
    return [d["day"] for d in plan["schedule"] if code in (d[m]["code"] for m in MEALS)]


# --------------------------------------------------------------------------- ingredients

_NUM = r"(?:\d+(?:\.\d+)?(?:\s?[½¼¾⅓⅔])?|[½¼¾⅓⅔])"
_FRAC = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3}
_SCALABLE = re.compile(rf"(?P<lo>{_NUM})(?:\s*(?P<dash>[–-])\s*(?P<hi>{_NUM}))?(?P<sp>\s*)"
                       r"(?P<unit>kg|g|ml|L|l|cups?|tbsp|tsp|inch|slices?|scoops?|bunch(?:es)?)?(?=\b|\s|$)")
_NOT_AMOUNT = re.compile(r"^\s*(?:h\b|min\b|whistles?\b|s\b|°|%|:|x\b|×)")
STAPLES = re.compile(r"\b(salt|oil|water|turmeric|chilli powder|red chilli|cumin|garam masala|spices?|ajwain|"
                     r"mustard seeds|curry leaves|black pepper|pepper|amchur|coriander powder|kasuri methi|"
                     r"tikka masala|chole masala|rajma masala|sambar powder|tamarind|soy sauce|cinnamon|ghee)\b", re.I)


def _num(s: str) -> float:
    s = s.strip()
    frac = sum(_FRAC[c] for c in s if c in _FRAC)
    digits = re.sub(r"[½¼¾⅓⅔\s]", "", s)
    return frac + (float(digits) if digits else 0.0)


def _fmt(x: float, unit: str | None) -> str:
    if unit in ("g", "ml"):
        step = 5 if x < 1000 else 50
        return f"{round(x / step) * step:g}"
    if unit in ("kg", "L", "l"):
        return f"{round(x, 2):g}"
    whole, rest = int(x), x - int(x)
    for v, sym in ((0, ""), (0.25, "¼"), (1 / 3, "⅓"), (0.5, "½"), (2 / 3, "⅔"), (0.75, "¾"), (1, "")):
        if abs(rest - v) < 0.07:
            whole += 1 if v == 1 else 0
            return (f"{whole}" if whole else "") + sym if (whole or sym) else "0"
    return f"{round(x, 1):g}"


def scale_text(text: str, factor: float) -> str:
    """Scale every written amount in an ingredient line ("1 cup", "300 g", "3–4 rotis"),
    leaving times and other numbers ("soaked 3 h", "1:2 water") alone."""
    if abs(factor - 1) < 1e-9:
        return text

    def repl(m: re.Match) -> str:
        if _NOT_AMOUNT.match(text[m.end():]) or (m.group("unit") is None and _NOT_AMOUNT.match(text[m.end():])):
            return m.group(0)
        unit = m.group("unit")
        lo = _fmt(_num(m.group("lo")) * factor, unit)
        out = lo
        if m.group("hi"):
            out += m.group("dash") + _fmt(_num(m.group("hi")) * factor, unit)
        return out + (m.group("sp") or "") + (unit or "")
    return _SCALABLE.sub(repl, text)


def ingredient_name(text: str) -> str:
    """'yellow moong dal 1 cup (soaked 3 h)' -> 'Yellow moong dal'."""
    s = re.sub(r"\([^)]*\)", "", text)
    s = re.sub(rf"{_NUM}(?:\s*[–-]\s*{_NUM})?\s*(?:kg|g|ml|L|l|cups?|tbsp|tsp|inch|slices?|scoops?|bunch(?:es)?)?\b", " ", s)
    s = re.sub(r"\b(dry|on the side|for cooking|to eat alongside|total|cubed|crumbled|boiled|squeezed)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.-+")
    return (s[:1].upper() + s[1:]) if s else text


def grocery_key(name: str) -> str:
    """Compare shopping-list names without brackets or case: 'Lobia (black-eyed peas)' == 'lobia'."""
    return re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", "", name)).strip().casefold()


def aggregate(items: list[dict]) -> list[dict]:
    """Combine the same ingredient across recipes only when the units match;
    different units ("1 cup" vs "200 g") stay as separate lines."""
    out: dict[tuple, dict] = {}
    for it in items:
        key = (it["name"].casefold(), it.get("unit"))
        if key in out and it.get("qty") is not None and out[key].get("qty") is not None:
            row = out[key]
            row["qty"] = round(row["qty"] + it["qty"], 3)
            if row.get("qty_max") is not None or it.get("qty_max") is not None:
                row["qty_max"] = round((row.get("qty_max") or row["qty"]) + (it.get("qty_max") or it["qty"]), 3)
            row["recipes"] = sorted(set(row["recipes"] + it["recipes"]))
            row["sources"].extend(it["sources"])
        elif key in out:
            out[(key[0], key[1], len(out))] = it                     # can't add "a handful" to "1 cup"
        else:
            out[key] = {**it, "sources": list(it["sources"])}
    for row in out.values():
        if row.get("qty") is not None:
            q = _fmt(row["qty"], row.get("unit"))
            if row.get("qty_max") is not None and row["qty_max"] != row["qty"]:
                q += "–" + _fmt(row["qty_max"], row.get("unit"))
            row["quantity"] = f"{q} {row['unit']}" if row.get("unit") else q
        else:
            row["quantity"] = None
    return sorted(out.values(), key=lambda r: (r["staple"], r["name"].casefold()))


def ingredients_for(plan: dict, codes: list[str]) -> list[dict]:
    items = []
    for code in codes:
        for ing in plan["recipes"][code]["ingredients"]:
            items.append({
                "name": ingredient_name(ing["text"]), "qty": ing.get("qty"), "qty_max": ing.get("qty_max"),
                "unit": ing.get("unit"), "recipes": [code], "sources": [f"{code}: {ing['text']}"],
                "staple": bool(STAPLES.search(ing["text"])) and ing.get("unit") in (None, "tsp", "tbsp"),
            })
    return aggregate(items)


# --------------------------------------------------------------------------- preparation

def prep_tasks(plan: dict, target: date) -> list[dict]:
    """Preparation for ``target``'s meals: what to do the evening before ("tonight")
    and on the day ("tomorrow"). Only from the recipes' own instructions, plus two
    checks the plan recommends in its meal-prep section (curd, key ingredients)."""
    entry = day_entry(plan, target)
    tasks: list[dict] = []
    seen = set()
    for meal in MEALS:
        code = entry[meal]["code"]
        for rule in plan["prep"].get(code, []):
            tid = f"{code}:{rule['id']}"
            if tid in seen:
                continue
            seen.add(tid)
            tasks.append({"id": tid, "meal": meal, "recipe": code, "when": rule["when"], "kind": rule["kind"],
                          "text": rule["text"], "source": rule["source"], "unstated": bool(rule.get("unstated")),
                          "hindi": rule.get("hindi")})
    codes = [entry[m]["code"] for m in MEALS]
    curd = [(c, i) for c in codes for i in plan["recipes"][c]["ingredients"]
            if re.search(r"\bcurd\b", i["text"], re.I) and i.get("unit") == "g"]
    if curd:
        total = sum(i["qty"] for _, i in curd)
        parts = ", ".join(f"{c} {i['qty']:g} g" for c, i in curd)
        tasks.append({"id": "check:curd", "meal": None, "recipe": None, "when": "tonight", "kind": "check",
                      "text": f"Tomorrow's recipes use about {total:g} g curd ({parts}), plus any extra curd in "
                              "Person 1's add-ons. Check you have enough, or set curd tonight.",
                      "source": "Curd & sprouts: set curd every 2 days", "unstated": False, "hindi": None})
    key = re.compile(r"\b(paneer|tofu|soya|rajma|chana|lobia|dal|besan|oats|poha|quinoa|batter|bread|mushrooms?)\b", re.I)
    needed = [r for r in ingredients_for(plan, codes) if key.search(r["name"]) and not r["staple"] and r["quantity"]]
    if needed:
        listing = "; ".join(f"{r['name']} {r['quantity']}" if r["quantity"] else r["name"] for r in needed)
        tasks.append({"id": "check:key-ingredients", "meal": None, "recipe": None, "when": "tonight", "kind": "check",
                      "text": f"Check the kitchen has: {listing}.",
                      "source": "from tomorrow's recipe ingredients", "unstated": False, "hindi": None})
    order = {"tonight": 0, "tomorrow": 1}
    return sorted(tasks, key=lambda t: (order[t["when"]], 0 if t["recipe"] else 1))


# --------------------------------------------------------------------------- records (SQLite)

class NutritionLog:
    """Daily activity, keyed by real calendar date (never by plan day)."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS meal_log (
                date TEXT NOT NULL, meal TEXT NOT NULL, person TEXT NOT NULL, done INTEGER NOT NULL,
                plan_day INTEGER NOT NULL, recipe TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (date, meal, person));
            CREATE TABLE IF NOT EXISTS prep_log (
                date TEXT NOT NULL, task TEXT NOT NULL, done INTEGER NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (date, task));
            CREATE TABLE IF NOT EXISTS plan_grocery (
                week TEXT NOT NULL, item TEXT NOT NULL, purchased INTEGER NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (week, item));
            CREATE TABLE IF NOT EXISTS day_note (
                date TEXT PRIMARY KEY, text TEXT NOT NULL, updated_at TEXT NOT NULL);
        """)
        make_private(db_path)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    def set_meal(self, d: date, meal: str, person: str, done: bool, day: int, recipe: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO meal_log VALUES (?,?,?,?,?,?,?) ON CONFLICT(date, meal, person) "
                             "DO UPDATE SET done=excluded.done, updated_at=excluded.updated_at",
                             (d.isoformat(), meal, person, int(done), day, recipe, self._now()))

    def meals(self, d: date) -> dict:
        rows = self._db.execute("SELECT meal, person, done FROM meal_log WHERE date = ?", (d.isoformat(),)).fetchall()
        out = {m: {p: False for p in PEOPLE} for m in MEALS}
        for meal, person, done in rows:
            if meal in out and person in out[meal]:
                out[meal][person] = bool(done)
        return out

    def set_prep(self, d: date, task: str, done: bool) -> None:
        with self._lock:
            self._db.execute("INSERT INTO prep_log VALUES (?,?,?,?) ON CONFLICT(date, task) "
                             "DO UPDATE SET done=excluded.done, updated_at=excluded.updated_at",
                             (d.isoformat(), task, int(done), self._now()))

    def prep(self, d: date) -> dict[str, bool]:
        rows = self._db.execute("SELECT task, done FROM prep_log WHERE date = ?", (d.isoformat(),)).fetchall()
        return {t: bool(done) for t, done in rows}

    def set_grocery(self, week: str, item: str, purchased: bool) -> None:
        with self._lock:
            self._db.execute("INSERT INTO plan_grocery VALUES (?,?,?,?) ON CONFLICT(week, item) "
                             "DO UPDATE SET purchased=excluded.purchased, updated_at=excluded.updated_at",
                             (week, item, int(purchased), self._now()))

    def groceries(self, week: str) -> dict[str, bool]:
        rows = self._db.execute("SELECT item, purchased FROM plan_grocery WHERE week = ?", (week,)).fetchall()
        return {i: bool(p) for i, p in rows}

    def set_note(self, d: date, text: str) -> None:
        with self._lock:
            if text.strip():
                self._db.execute("INSERT INTO day_note VALUES (?,?,?) ON CONFLICT(date) "
                                 "DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
                                 (d.isoformat(), text.strip(), self._now()))
            else:
                self._db.execute("DELETE FROM day_note WHERE date = ?", (d.isoformat(),))

    def note(self, d: date) -> str:
        row = self._db.execute("SELECT text FROM day_note WHERE date = ?", (d.isoformat(),)).fetchone()
        return row[0] if row else ""

    def history(self, start: date, end: date) -> list[dict]:
        rows = self._db.execute(
            "SELECT date, meal, person, done, plan_day, recipe FROM meal_log WHERE date BETWEEN ? AND ? ORDER BY date",
            (start.isoformat(), end.isoformat())).fetchall()
        return [{"date": r[0], "meal": r[1], "person": r[2], "done": bool(r[3]), "plan_day": r[4], "recipe": r[5]}
                for r in rows]


# --------------------------------------------------------------------------- views

def meal_card(plan: dict, entry: dict, meal: str) -> dict:
    code = entry[meal]["code"]
    r = plan["recipes"][code]
    return {"meal": meal, "code": code, "label": entry[meal]["label"], "name": r["name"], "paneer": r["paneer"],
            "base_serving": r["base_serving"], "person1_extra": r["person1_extra"]}


def training_for(plan: dict, d: date) -> dict:
    wd = WEEKDAYS[d.weekday()]
    week = plan_week(plan_day(d))
    out = {"week": week}
    for p in PEOPLE:
        t = plan["context"]["training"][p]
        out[p] = {"session": t["days"].get(wd), "rest": t["rest"], "week_note": t["weeks"][str(week)]
                  if str(week) in t["weeks"] else t["weeks"].get(week)}
    return out


def day_view(plan: dict, log: NutritionLog, d: date) -> dict:
    entry = day_entry(plan, d)
    tomorrow = d + timedelta(days=1)
    t_entry = day_entry(plan, tomorrow)
    done = log.prep(tomorrow)
    tasks = [{**t, "done": done.get(t["id"], False)} for t in prep_tasks(plan, tomorrow)]
    return {
        "date": d.isoformat(), "plan_day": entry["day"], "week": entry["week"], "weekday": d.strftime("%A"),
        "month_restart": d.day == 1, "beyond_plan": d.day > LAST_PLAN_DAY,
        "meals": [meal_card(plan, entry, m) for m in MEALS],
        "totals": {"kcal": entry["kcal"], "protein_g": entry["protein_g"]},
        "eaten": log.meals(d),
        "note": log.note(d),
        "week_note": next((w["note"] for w in plan["weeks"] if w["week"] == entry["week"]), None),
        "tomorrow": {
            "date": tomorrow.isoformat(), "plan_day": t_entry["day"], "weekday": tomorrow.strftime("%A"),
            "month_restart": tomorrow.day == 1,
            "meals": [meal_card(plan, t_entry, m) for m in MEALS],
            "tasks": tasks,
        },
        "training": training_for(plan, d),
    }


def recipe_view(plan: dict, code: str, servings: float) -> dict:
    r = plan["recipes"][code]
    base = r.get("batch_servings") or 2
    factor = servings / base
    return {
        **r,
        "servings": servings, "factor": round(factor, 3),
        "scaled_ingredients": [{"text": i["text"], "scaled": scale_text(i["text"], factor),
                                "scalable": i.get("qty") is not None} for i in r["ingredients"]],
        "prep": plan["prep"].get(code, []),
        "used_on_days": used_on_days(plan, code),
    }
