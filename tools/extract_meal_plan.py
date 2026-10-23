"""Build app/data/meal_plan.json from "1-Month Body Recomposition Plan.pdf".

    .venv/bin/python tools/extract_meal_plan.py path/to/plan.pdf

Recipes (codes, names, macros, ingredients, method, Person 1 extras, notes) are
parsed from the PDF's own text (pdftotext). The 30-day schedule, grocery lists
and the training/recovery sections are transcribed in tools/meal_plan_source.py
and cross-checked here against the same text. Nothing is generated or guessed:
fields the PDF doesn't state are left out, and every check that fails stops the
build. Requires poppler's pdftotext (already on the Pi).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import meal_plan_source as src  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "app" / "data" / "meal_plan.json"

HEADER = re.compile(
    r"(?P<code>[BLD]\d{1,2}) — (?P<title>[^.]+?)\.\s+(?P<paneer>paneer\s+)?Base(?: serving)?:\s*"
    r"(?P<kcal>\d+) kcal • (?P<p>\d+) g P • (?P<c>\d+) g C • (?P<f>\d+) g F • (?P<fib>\d+) g fibre\.")
EXTRA_NUMS = re.compile(r"\(\+(?P<kcal>\d+) kcal, \+(?P<p>\d+) g P\)\.?")
HINDI_WORDS = {"ko", "karo", "hai", "nahi", "warna", "zyada", "thodi", "rakho", "phir", "taaki", "bane", "badhta",
               "dabakar", "nikaalo", "lagta", "halka", "daba", "kar", "bhuno", "failao", "paani"}
STOP = re.compile(r"\s(?:Week \d+ (?:Recipes|New Recipes?)|Week \d+ \(Days|Variety check|Section \d)\b")


def pdf_text(pdf: Path) -> str:
    return subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True, check=True).stdout


def flat(text: str) -> str:
    text = text.replace(" ", " ").replace("\x0c", " ")
    return re.sub(r"\s+", " ", text)


def split_items(s: str) -> list[str]:
    """Split an ingredient list on , and ; outside brackets."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch in ",;" and depth == 0:
            if cur.strip():
                out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip().rstrip("."))
    return out


FRAC = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3}
NUM = r"(?:\d+(?:\.\d+)?\s*[½¼¾⅓⅔]?|[½¼¾⅓⅔])"
UNITS = r"(?:kg|g|ml|L|l|cups?|tbsp|tsp|inch|slices?|scoops?|bunch|bunches)"
QTY = re.compile(rf"(?P<lo>{NUM})(?:\s*[–-]\s*(?P<hi>{NUM}))?\s*(?P<unit>{UNITS})?\b")


def num(s: str) -> float:
    s = s.strip()
    total = 0.0
    for ch in s:
        if ch in FRAC:
            total += FRAC[ch]
    digits = re.sub(r"[½¼¾⅓⅔\s]", "", s)
    return total + (float(digits) if digits else 0.0)


def ingredient(raw: str) -> dict:
    """{text, qty?, qty_max?, unit?} - only when the quantity is written in the PDF."""
    item: dict = {"text": raw}
    m = QTY.search(raw)
    if m and not re.match(r"^\s*(\d+\s*(?:–|-)\s*\d+\s*(?:min|whistles))", raw[m.start():]):
        lo = num(m.group("lo"))
        if lo > 0:
            item["qty"] = round(lo, 3)
            if m.group("hi"):
                item["qty_max"] = round(num(m.group("hi")), 3)
            if m.group("unit"):
                item["unit"] = m.group("unit")
    return item


def parse_recipes(text: str) -> dict:
    t = flat(text)
    heads = list(HEADER.finditer(t))
    recipes = {}
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(t)
        body = t[h.end():end]
        stop = STOP.search(body)
        if stop:
            body = body[:stop.start()]
        ing_m = re.search(r"Ingredients \((?P<n>[^)]*)\):\s*(?P<ing>.*?)\s*Method:\s*(?P<method>.*?)\s*Person 1 extra:\s*(?P<extra>.*)$", body)
        if not ing_m:
            raise SystemExit(f"{h['code']}: couldn't find Ingredients/Method/Person 1 extra")
        extra = ing_m["extra"].strip()
        em = EXTRA_NUMS.search(extra)
        extra_text = extra[:em.start()].strip() if em else extra
        trailing = extra[em.end():].strip() if em else ""
        cont = re.match(r"^([a-z][^.]*\.)\s*", trailing)        # "(+120 kcal, +24 g P) and 1 extra chilla."
        if cont:
            extra_text = f"{extra_text} {cont.group(1).rstrip('.')}"
            trailing = trailing[cont.end():]
        notes = []
        for sentence in re.split(r"(?<=[.!])\s+(?=[A-Z])", trailing):
            sentence = sentence.strip()
            if not sentence:
                continue
            words = set(re.findall(r"[a-z]+", sentence.lower()))
            notes.append({"text": sentence, "lang": "hi" if len(words & HINDI_WORDS) >= 2 else "en"})
        n = ing_m["n"].strip()
        servings = int(re.match(r"\d+", n).group()) if re.match(r"\d+", n) else None
        code = h["code"]
        recipes[code] = {
            "code": code,
            "meal": {"B": "breakfast", "L": "lunch", "D": "dinner"}[code[0]],
            "name": h["title"].strip(),
            "paneer": bool(h["paneer"]),
            "base_serving": {"kcal": int(h["kcal"]), "protein_g": int(h["p"]), "carbs_g": int(h["c"]),
                             "fat_g": int(h["f"]), "fibre_g": int(h["fib"])},
            "batch_servings": servings,
            "ingredients_text": ing_m["ing"].strip(),
            "ingredients": [ingredient(x) for x in split_items(ing_m["ing"].strip())],
            "method": [s.strip() for s in re.split(r"(?<=[.])\s+(?=[A-Z])", ing_m["method"].strip()) if s.strip()],
            "person1_extra": {"text": extra_text.rstrip("."),
                              **({"kcal": int(em["kcal"]), "protein_g": int(em["p"])} if em else {})},
            "notes": notes,
        }
    return recipes


def layout_text(pdf: Path) -> str:
    return subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, check=True).stdout


def words(s: str) -> list[str]:
    s = re.sub(r"paneer(?=[A-Z])", "", s)          # the PDF's small "paneer" badge, glued to the dish name
    return re.findall(r"[a-z0-9½¼¾×]+", s.lower())


def schedule_rows(layout: str) -> dict[int, str]:
    """Day number -> the text block of its row in the Section 2 tables (rows are separated by blank lines)."""
    sec = layout[layout.index("Section 2"):layout.index("Section 3")]
    lines = sec.splitlines()
    rows: dict[int, str] = {}
    expected = 1
    for i, line in enumerate(lines):
        m = re.match(r"^\s{0,3}(\d{1,2})\s{3,}\S", line)
        if m and int(m.group(1)) == expected:
            lo = i
            while lo > 0 and lines[lo - 1].strip():
                lo -= 1
            hi = i
            while hi + 1 < len(lines) and lines[hi + 1].strip():
                hi += 1
            rows[expected] = "\n".join(lines[lo:hi + 1])
            expected += 1
    return rows


def validate(plan: dict, text: str, layout: str) -> list[str]:
    problems = []
    recipes = plan["recipes"]
    if len(recipes) != 36:
        problems.append(f"expected 36 recipes, found {len(recipes)}")
    for meal, prefix in (("breakfast", "B"), ("lunch", "L"), ("dinner", "D")):
        codes = {c for c in recipes if c.startswith(prefix)}
        want = {f"{prefix}{i}" for i in range(1, 13)}
        if codes != want:
            problems.append(f"{meal}: missing {sorted(want - codes)} extra {sorted(codes - want)}")
    for code, r in recipes.items():
        for field in ("ingredients", "method"):
            if not r[field]:
                problems.append(f"{code}: empty {field}")
    days = plan["schedule"]
    if [d["day"] for d in days] != list(range(1, 31)):
        problems.append("schedule must list days 1-30 in order")
    rows = schedule_rows(layout)
    if sorted(rows) != list(range(1, 31)):
        problems.append(f"found schedule rows for days {sorted(rows)} in the PDF")
    for d in days:
        block = rows.get(d["day"], "")
        bwords = words(block)
        for meal in ("breakfast", "lunch", "dinner"):
            code = d[meal]["code"]
            if code not in recipes:
                problems.append(f"day {d['day']} {meal}: {code} has no recipe")
            elif recipes[code]["meal"] != meal:
                problems.append(f"day {d['day']} {meal}: {code} is a {recipes[code]['meal']} recipe")
            if f"({code})" not in block.replace("\n", " ") and not re.search(rf"\({code}\)", re.sub(r"\s+", "", block)):
                problems.append(f"day {d['day']} {meal}: code {code} not in that row of the PDF")
            missing = [w for w in words(d[meal]["label"]) if w not in bwords]
            if missing:
                problems.append(f"day {d['day']} {meal}: words {missing} of '{d[meal]['label']}' not in that row")
        p1, p2 = d["kcal"]["person1"], d["kcal"]["person2"]
        m = re.search(r"(\d{4})/(\d{2,3})", block)
        rest = str(p2)[len(m.group(2)):] if m else None
        if not m or m.group(1) != str(p1) or not str(p2).startswith(m.group(2)) or \
                (rest and not re.search(rf"(?<![\d/]){rest}(?![\d/])", block)):
            problems.append(f"day {d['day']}: calories {p1}/{p2} don't match the PDF row")
        prot = f"{d['protein_g']['person1']}/{d['protein_g']['person2']}"
        if prot not in block:
            problems.append(f"day {d['day']}: protein {prot} doesn't match the PDF row")
    sec3 = layout[layout.index("Section 3"):layout.index("Section 4")]
    week_secs = re.split(r"\n\s*Week (\d)\s*\n", sec3)
    by_week = {int(week_secs[i]): week_secs[i + 1] for i in range(1, len(week_secs) - 1, 2)}
    for w in plan["groceries"]["weeks"]:
        sw = words(by_week.get(w["week"], ""))
        for cat in w["categories"]:
            for item in cat["items"]:
                missing = [x for x in words(item["text"]) if x not in sw]
                if missing:
                    problems.append(f"week {w['week']} grocery '{item['text']}': {missing} not in that week's list")
    return problems


def main() -> None:
    pdf = Path(sys.argv[1])
    text = pdf_text(pdf)
    layout = layout_text(pdf)
    plan = {
        "version": 1,
        "source": {"title": "1-Month Body Recomposition Plan", "file": pdf.name,
                   "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(), "pages": 19},
        "people": src.PEOPLE,
        "portion_rule": src.PORTION_RULE,
        "disclaimer": src.DISCLAIMER,
        "schedule": src.schedule(),
        "weeks": src.WEEKS,
        "recipes": parse_recipes(text),
        "recipe_overrides": {},
        "prep": src.PREP,
        "groceries": src.groceries(),
        "context": src.CONTEXT,
    }
    for code, fix in src.RECIPE_FIXES.items():          # documented corrections of extraction artefacts
        plan["recipes"][code].update(fix)
    problems = validate(plan, text, layout)
    if problems:
        print("VALIDATION FAILED:")
        for p in problems:
            print(" -", p)
        raise SystemExit(1)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n")
    print(f"wrote {OUT} - {len(plan['recipes'])} recipes, {len(plan['schedule'])} days, "
          f"{sum(len(c['items']) for w in plan['groceries']['weeks'] for c in w['categories'])} grocery items")


if __name__ == "__main__":
    main()
