"""Hand-transcribed parts of "1-Month Body Recomposition Plan.pdf" (schedule tables,
grocery lists, training/recovery sections) for tools/extract_meal_plan.py, which
checks every schedule label, calorie/protein figure and grocery item against the
PDF's own text before writing app/data/meal_plan.json.

Prep rules: each one quotes the recipe text that makes it necessary ("source").
Where the PDF gives no duration ("kabuli chana 1 cup dry (soaked)"), the rule says
so instead of inventing one.
"""

PEOPLE = {
    "person1": {"label": "Person 1", "summary": "Male, 32, 178 cm, 85 kg, Intermediate"},
    "person2": {"label": "Person 2", "summary": "Female, 32, 160 cm, 70 kg, Beginner"},
}

PORTION_RULE = {
    "text": ("The recipe macros are shown per base serving = Person 2's plate. Person 1 hits his higher target by "
             "adding the listed extras each meal (typically +1 scoop whey or extra dal/curd at breakfast, and +1 cup "
             "cooked rice OR +2 rotis at lunch and dinner). Roughly: Person 2 eats the base plate; Person 1 eats the "
             "base plate ×1.4."),
    "hindi": ("Khana ek saath banega. Person 2 normal plate lega, Person 1 usi plate me thoda extra roti/rice aur "
              "protein add karega."),
    "how_to_use": ("Cooking is shared — recipes make enough for both people. Because Person 1 needs more energy and "
                   "protein than Person 2, each meal lists a base serving (Person 2's portion) and simple Person 1 "
                   "add-ons (extra roti/rice + a protein topper)."),
    "plating_tip": ("Portion as you cook: plate Person 2's base serving first, then add Person 1's extras to the "
                    "pan/plate — keeps macros on track without weighing every day."),
}

DISCLAIMER = ("This plan is for general education and healthy adults. It is not medical or personalised dietetic "
              "advice. Anyone with a medical condition, or who is pregnant/breastfeeding, should consult a doctor or "
              "registered dietitian before starting.")

# day, (breakfast label, code), (lunch label, code), (dinner label, code), kcal P1/P2, protein P1/P2
_ROWS = [
    (1, ("Moong dal chilla + curd", "B1"), ("Rajma + brown rice + salad", "L1"), ("Tofu bhurji + 2 roti", "D2"), (2300, 1580), (184, 130)),
    (2, ("Masala oats + peanuts", "B3"), ("Dal tadka + roti + sabzi", "L3"), ("Soya veg curry + roti", "D3"), (2290, 1570), (180, 126)),
    (3, ("Protein poha + sprouts", "B6"), ("Chole + jeera rice", "L2"), ("Dal + palak sabzi + roti", "D5"), (2310, 1590), (178, 124)),
    (4, ("Paneer bhurji + roti", "B4"), ("Soya chunk curry + rice", "L4"), ("Light rajma + salad + curd", "D6"), (2300, 1585), (186, 132)),
    (5, ("Besan chilla + chutney + curd", "B2"), ("Kala chana masala + roti", "L5"), ("Tofu tikka masala + roti", "D8"), (2295, 1575), (185, 131)),
    (6, ("Idli + sambar + chutney", "B8"), ("Paneer sabzi + roti", "L6"), ("Lauki + moong dal + roti", "D11"), (2280, 1560), (176, 122)),
    (7, ("Protein smoothie + nuts", "B10"), ("Moong dal khichdi + curd", "L7"), ("Sprout & grilled tofu bowl", "D12"), (2290, 1575), (181, 128)),
    (8, ("Tofu scramble + toast", "B5"), ("Chole + jeera rice", "L2"), ("Paneer tikka + veg + roti", "D1"), (2300, 1585), (186, 132)),
    (9, ("Overnight oats + curd + PB", "B7"), ("Dal tadka + roti + sabzi", "L3"), ("Soya veg curry + roti", "D3"), (2290, 1575), (182, 128)),
    (10, ("Oats dosa + chana sambar", "B9"), ("Lobia curry + rice", "L8"), ("Dal + palak sabzi + roti", "D5"), (2295, 1580), (178, 125)),
    (11, ("Veg uttapam + chana dal", "B11"), ("Chana palak + roti", "L10"), ("Tofu bhurji + 2 roti", "D2"), (2285, 1570), (181, 128)),
    (12, ("Masala oats + peanuts", "B3"), ("Soya chunk curry + rice", "L4"), ("Mushroom-peas + paneer bhurji + roti", "D10"), (2305, 1590), (185, 131)),
    (13, ("Soya keema paratha + curd", "B12"), ("Kala chana masala + roti", "L5"), ("Chana dal + veg + roti", "D7"), (2300, 1585), (184, 130)),
    (14, ("Protein smoothie + nuts", "B10"), ("Sambar rice + veg", "L9"), ("Sprout & grilled tofu bowl", "D12"), (2290, 1575), (180, 127)),
    (15, ("Moong dal chilla + curd", "B1"), ("Rajma + brown rice + salad", "L1"), ("Tofu tikka masala + roti", "D8"), (2295, 1580), (184, 130)),
    (16, ("Protein poha + sprouts", "B6"), ("Dal tadka + roti + sabzi", "L3"), ("Moong dal soup + grilled paneer salad", "D4"), (2300, 1585), (183, 129)),
    (17, ("Idli + sambar + chutney", "B8"), ("Tofu veg stir-fry + rice", "L11"), ("Dal + palak sabzi + roti", "D5"), (2290, 1575), (181, 128)),
    (18, ("Besan chilla + chutney + curd", "B2"), ("Mixed dal + quinoa + salad", "L12"), ("Soya veg curry + roti", "D3"), (2295, 1580), (182, 129)),
    (19, ("Paneer bhurji + roti", "B4"), ("Kala chana masala + roti", "L5"), ("Tofu bhurji + 2 roti", "D2"), (2305, 1590), (187, 133)),
    (20, ("Overnight oats + curd + PB", "B7"), ("Lobia curry + rice", "L8"), ("Lauki + moong dal + roti", "D11"), (2285, 1570), (178, 125)),
    (21, ("Protein smoothie + nuts", "B10"), ("Moong dal khichdi + curd", "L7"), ("Light rajma + salad + curd", "D6"), (2290, 1575), (180, 127)),
    (22, ("Masala oats + peanuts", "B3"), ("Chole + jeera rice", "L2"), ("Paneer tikka + veg + roti", "D1"), (2300, 1585), (184, 130)),
    (23, ("Tofu scramble + toast", "B5"), ("Chana palak + roti", "L10"), ("Soya veg curry + roti", "D3"), (2290, 1575), (183, 129)),
    (24, ("Oats dosa + chana sambar", "B9"), ("Soya chunk curry + rice", "L4"), ("Dal + palak sabzi + roti", "D5"), (2295, 1580), (182, 128)),
    (25, ("Veg uttapam + chana dal", "B11"), ("Dal tadka + roti + sabzi", "L3"), ("Savory besan chilla + curd + salad", "D9"), (2285, 1570), (180, 127)),
    (26, ("Besan chilla + chutney + curd", "B2"), ("Paneer sabzi + roti", "L6"), ("Chana dal + veg + roti", "D7"), (2305, 1590), (185, 131)),
    (27, ("Soya keema paratha + curd", "B12"), ("Rajma + brown rice + salad", "L1"), ("Sprout & grilled tofu bowl", "D12"), (2300, 1585), (184, 130)),
    (28, ("Protein smoothie + nuts", "B10"), ("Sambar rice + veg", "L9"), ("Tofu bhurji + 2 roti", "D2"), (2290, 1575), (181, 128)),
    (29, ("Overnight oats + curd + PB", "B7"), ("Kala chana masala + roti", "L5"), ("Tofu tikka masala + roti", "D8"), (2295, 1580), (183, 129)),
    (30, ("Idli + sambar + chutney", "B8"), ("Mixed dal + quinoa + salad", "L12"), ("Moong dal soup + grilled paneer salad", "D4"), (2300, 1585), (182, 129)),
]


def _week(day: int) -> int:
    return 1 if day <= 7 else 2 if day <= 14 else 3 if day <= 21 else 4


def schedule() -> list[dict]:
    out = []
    for day, b, l, d, kcal, prot in _ROWS:
        out.append({
            "day": day, "week": _week(day),
            "breakfast": {"label": b[0], "code": b[1]},
            "lunch": {"label": l[0], "code": l[1]},
            "dinner": {"label": d[0], "code": d[1]},
            "kcal": {"person1": kcal[0], "person2": kcal[1]},
            "protein_g": {"person1": prot[0], "person2": prot[1]},
        })
    return out


WEEKS = [
    {"week": 1, "days": [1, 7], "title": "Week 1 (Days 1–7)", "note": None},
    {"week": 2, "days": [8, 14], "title": "Week 2 (Days 8–14)", "note": None},
    {"week": 3, "days": [15, 21], "title": "Week 3 (Days 15–21)", "note": None},
    {"week": 4, "days": [22, 30], "title": "Week 4 (Days 22–30) — Diet steady through training deload",
     "note": ("Keep calories the same in Week 4. Training volume drops (deload) but protein stays high to protect "
              "muscle. If very hungry on rest days, add an extra bowl of salad or 150 g curd — free foods.")},
]

# Advance preparation, per recipe. when: "tonight" (the evening before) or "tomorrow" (on the day,
# before cooking). "source" quotes the recipe in the PDF; "unstated" marks a missing duration.
PREP = {
    "B1": [{"id": "soak-moong", "when": "tonight", "kind": "soak",
            "text": "Plan the soak: 1 cup yellow moong dal must be soaked 3 h before you blend the batter (start tonight or early morning).",
            "source": "yellow moong dal 1 cup (soaked 3 h)"}],
    "B6": [{"id": "sprouts", "when": "tonight", "kind": "check",
            "text": "Check that 1 cup moong sprouts are ready.", "source": "moong sprouts 1 cup"}],
    "B7": [{"id": "overnight-oats", "when": "tonight", "kind": "advance",
            "text": "Mix 1 cup rolled oats, 250 g hung/Greek-style curd, 200 ml milk and 1 tbsp chia in a jar; refrigerate overnight.",
            "source": "Mix oats, curd, milk, chia in a jar; refrigerate overnight."},
           {"id": "overnight-oats-top", "when": "tomorrow", "kind": "cook",
            "text": "Morning: top the oats with banana, peanut butter and fruit.",
            "source": "Morning: top with banana, peanut butter, fruit."}],
    "B8": [{"id": "idli-batter", "when": "tonight", "kind": "check",
            "text": "Check you have idli batter for 6–8 idlis (ready batter or homemade).",
            "source": "6–8 idlis (ready batter or homemade)"}],
    "B9": [{"id": "rest-batter", "when": "tomorrow", "kind": "cook",
            "text": "Whisk the oats-rava batter first and let it rest 15 min before cooking.",
            "source": "Whisk a thin pourable batter, rest 15 min."}],
    "B11": [{"id": "uttapam-batter", "when": "tonight", "kind": "check",
             "text": "Check you have 2 cups dosa/idli batter.", "source": "dosa/idli batter 2 cups"},
            {"id": "boil-chana-dal", "when": "tonight", "kind": "advance",
             "text": "Can be done tonight: boil ½ cup chana dal (it is stirred into the batter).",
             "source": "boiled chana dal ½ cup mixed in batter for protein"}],
    "B12": [{"id": "soak-soya-granules", "when": "tomorrow", "kind": "soak", "unstated": True,
             "text": "Soak ¾ cup soya granules, then squeeze them (the plan doesn't state how long to soak).",
             "source": "soya granules ¾ cup (soaked, squeezed)"}],
    "L1": [{"id": "soak-rajma", "when": "tonight", "kind": "soak",
            "text": "Soak 1 cup dry rajma overnight.", "source": "rajma 1 cup dry (soaked overnight)"}],
    "L2": [{"id": "soak-chole", "when": "tonight", "kind": "soak", "unstated": True,
            "text": "Soak 1 cup dry kabuli chana (the recipe uses soaked chana; the plan doesn't state how long).",
            "source": "kabuli chana 1 cup dry (soaked)"}],
    "L4": [{"id": "boil-soya", "when": "tomorrow", "kind": "cook",
            "text": "Boil 1.5 cups soya chunks for 5 min, then squeeze out the water.",
            "source": "Boil soya chunks 5 min, squeeze out water.", "hindi": "Soya ko dabakar paani nikaalo, warna kaccha lagta hai."}],
    "L5": [{"id": "soak-kala-chana", "when": "tonight", "kind": "soak", "unstated": True,
            "text": "Soak 1 cup dry kala chana (the recipe uses soaked chana; the plan doesn't state how long).",
            "source": "kala chana 1 cup dry (soaked)"}],
    "L8": [{"id": "soak-lobia", "when": "tonight", "kind": "soak", "unstated": True,
            "text": "Soak 1 cup dry lobia (the recipe uses soaked lobia; the plan doesn't state how long).",
            "source": "lobia 1 cup dry (soaked)"}],
    "L10": [{"id": "soak-chole", "when": "tonight", "kind": "soak", "unstated": True,
             "text": "Soak 1 cup dry kabuli chana (the recipe uses soaked chana; the plan doesn't state how long).",
             "source": "kabuli chana 1 cup dry (soaked)"}],
    "D1": [{"id": "marinate-paneer", "when": "tomorrow", "kind": "advance",
            "text": "Marinate 200 g paneer in 100 g hung curd + tikka masala + ginger-garlic for 30 min before cooking.",
            "source": "Marinate paneer 30 min."}],
    "D2": [{"id": "press-tofu", "when": "tomorrow", "kind": "cook",
            "text": "Press the tofu lightly to remove water before cooking.",
            "source": "Tofu ko halka daba kar paani nikaalo phir bhuno."}],
    "D3": [{"id": "boil-soya", "when": "tomorrow", "kind": "cook",
            "text": "Boil and squeeze 1.5 cups soya chunks.", "source": "soya chunks 1.5 cups (boiled/squeezed)"}],
    "D6": [{"id": "soak-rajma", "when": "tonight", "kind": "soak",
            "text": "Soak ¾ cup dry rajma overnight (it is cooked as in L1).",
            "source": "rajma ¾ cup dry (cooked as L1 but no rice); L1: rajma 1 cup dry (soaked overnight)"}],
    "D8": [{"id": "marinate-tofu", "when": "tomorrow", "kind": "advance", "unstated": True,
            "text": "Marinate 250 g firm tofu in curd + tikka spices (the plan doesn't state for how long).",
            "source": "firm tofu 250 g cubed, marinated in curd + tikka spices"}],
    "D12": [{"id": "sprouts", "when": "tonight", "kind": "check",
             "text": "Check that 1.5 cups mixed sprouts are ready.", "source": "mixed sprouts 1.5 cups (steamed)"}],
}

# Weekly grocery lists, "for both people for one week and include Person 1's extra portions".
# category -> AURA grocery category for "Add to shopping list".
_GROCERY_CATS = {
    "Pulses / legumes": "pantry", "Soya / tofu / paneer": "dairy", "Grains": "pantry", "Dairy": "dairy",
    "Vegetables": "produce", "Sprouts": "produce", "Fruit": "produce", "Sprouts / fruit": "produce",
    "Nuts / seeds / fats": "pantry", "Optional supplement": "other",
}
_GROCERIES = {
    1: [
        ("Pulses / legumes", ["Rajma 300 g", "kabuli chana (chole) 250 g", "kala chana 200 g", "yellow moong dal 500 g",
                              "toor dal 400 g", "masoor dal 250 g", "besan 500 g"]),
        ("Soya / tofu / paneer", ["Soya chunks 250 g", "firm tofu 600 g", "paneer 450 g"]),
        ("Grains", ["Brown rice 500 g", "basmati rice 500 g", "rolled oats 500 g", "poha 250 g", "whole-wheat atta 2 kg",
                    "idli/dosa batter 1 kg (or rice+urad to make)", "multigrain bread 1 loaf"]),
        ("Dairy", ["Milk 5 L", "curd 1.5 kg (set fresh through week)", "1 pack hung/Greek-style curd if available"]),
        ("Vegetables", ["Onions 2 kg", "tomatoes 2 kg", "potatoes 500 g", "capsicum 500 g", "carrots 500 g", "beans 400 g",
                        "peas 400 g", "spinach (palak) 2 bunches", "bottle gourd (lauki) 1", "cucumber 1 kg", "lemon 10",
                        "ginger 200 g", "garlic 200 g", "green chilli 100 g", "coriander 3 bunches", "mint 1 bunch",
                        "drumstick/mixed sambar veg 500 g", "curry leaves 1 sprig-pack"]),
        ("Sprouts", ["Moong for sprouting 250 g (or ready sprouts 400 g)"]),
        ("Fruit", ["Bananas 7", "1 pack berries/1 apple ×4"]),
        ("Nuts / seeds / fats", ["Roasted peanuts 300 g", "almonds 150 g", "walnuts 100 g", "peanut butter 1 jar",
                                 "chia seeds 100 g", "cooking oil", "ghee 250 g", "5 cashews for gravy",
                                 "coconut (fresh/desiccated) 200 g"]),
        ("Optional supplement", ["Whey protein (see Section 6) — ~14 scoops/week if used by both"]),
    ],
    2: [
        ("Pulses / legumes", ["Kabuli chana 250 g", "kala chana 200 g", "lobia (black-eyed peas) 250 g", "toor dal 400 g",
                              "masoor dal 250 g", "moong dal 300 g", "chana dal 400 g", "besan 250 g"]),
        ("Soya / tofu / paneer", ["Soya chunks 200 g", "soya granules 200 g", "firm tofu 750 g", "paneer 350 g"]),
        ("Grains", ["Basmati rice 500 g", "oats 500 g", "atta 2 kg", "rava/sooji 250 g", "idli/dosa batter 1.5 kg",
                    "multigrain bread 1 loaf", "quinoa 250 g (optional)"]),
        ("Dairy", ["Milk 5 L", "curd 1.5 kg", "hung/Greek curd 500 g"]),
        ("Vegetables", ["Onions 2 kg", "tomatoes 2 kg", "capsicum 500 g", "mushrooms 200 g", "peas 400 g",
                        "spinach 3 bunches", "carrots 500 g", "beans 400 g", "cucumber 1 kg", "sambar veg 500 g",
                        "ginger 200 g", "garlic 200 g", "green chilli", "coriander 3 bunches", "mint 1 bunch",
                        "curry leaves", "lemon 10"]),
        ("Sprouts", ["Mixed sprouts 400 g"]),
        ("Fruit", ["Bananas 7", "apples/berries ×4"]),
        ("Nuts / seeds / fats", ["Peanuts 250 g", "almonds 100 g", "peanut butter (top up)", "chia 50 g",
                                 "ghee/oil top-up", "coconut for chutney/sambar 200 g"]),
    ],
    3: [
        ("Pulses / legumes", ["Rajma 300 g", "kala chana 200 g", "lobia 250 g", "moong dal 500 g", "toor dal 300 g",
                              "masoor dal 250 g", "chana dal 200 g", "besan 300 g"]),
        ("Soya / tofu / paneer", ["Soya chunks 200 g", "firm tofu 750 g", "paneer 350 g"]),
        ("Grains", ["Brown rice 500 g", "basmati 400 g", "quinoa 300 g", "oats 500 g", "atta 2 kg", "rava 150 g",
                    "idli/dosa batter 1 kg", "bread 1 loaf"]),
        ("Dairy", ["Milk 5 L", "curd 1.5 kg", "hung/Greek curd 500 g"]),
        ("Vegetables", ["Onions 2 kg", "tomatoes 2 kg", "spinach 3 bunches", "capsicum 500 g", "broccoli 300 g",
                        "beans 400 g", "carrots 500 g", "peas 300 g", "bottle gourd 1", "lettuce 1", "cucumber 1 kg",
                        "sambar veg 500 g", "ginger-garlic 200 g each", "chilli", "coriander", "mint", "curry leaves",
                        "lemon 10"]),
        ("Sprouts / fruit", ["Mixed sprouts 300 g", "bananas 7", "apples/berries ×4"]),
        ("Nuts / seeds / fats", ["Peanuts 250 g", "almonds 100 g", "walnuts 100 g", "peanut butter top-up", "chia 50 g",
                                 "cashews", "ghee/oil top-up", "coconut 150 g"]),
    ],
    4: [
        ("Pulses / legumes", ["Rajma 250 g", "kabuli chana 250 g", "kala chana 200 g", "moong dal 400 g", "toor dal 400 g",
                              "masoor dal 250 g", "chana dal 300 g", "besan 500 g"]),
        ("Soya / tofu / paneer", ["Soya chunks 200 g", "soya granules 200 g", "firm tofu 700 g", "paneer 400 g"]),
        ("Grains", ["Brown rice 400 g", "basmati 500 g", "quinoa 250 g", "oats 500 g", "atta 2 kg", "rava 150 g",
                    "idli/dosa batter 1.5 kg", "bread 1 loaf"]),
        ("Dairy", ["Milk 5 L", "curd 1.5 kg", "hung/Greek curd 500 g"]),
        ("Vegetables", ["Onions 2 kg", "tomatoes 2 kg", "spinach 3 bunches", "capsicum 500 g", "carrots 500 g",
                        "beans 400 g", "peas 400 g", "bottle gourd 1", "cucumber 1 kg", "lettuce 1", "sambar veg 500 g",
                        "ginger-garlic 200 g each", "chilli", "coriander", "mint", "curry leaves", "lemon 10"]),
        ("Sprouts / fruit", ["Mixed sprouts 300 g", "bananas 7", "apples/berries ×4"]),
        ("Nuts / seeds / fats", ["Peanuts 300 g", "almonds 100 g", "peanut butter top-up", "chia 50 g",
                                 "ghee/oil top-up", "coconut 150 g"]),
    ],
}

_QTY_TAIL = __import__("re").compile(
    r"^(?P<name>.*?)(?:\s+(?P<qty>(?:~?\d[\d.,]*\s*(?:kg|g|L|ml|bunch(?:es)?|loaf|jar|sprig-pack|scoops/week|×\d+)?|1 pack.*)"
    r"(?:\s*(?:\(.*\)|each|if used by both))?))?$")


def _split(text: str) -> tuple[str, str | None]:
    import re
    m = re.match(r"^(?P<name>[^\d(×~]+?)\s+(?P<qty>[~\d(×].*)$", text)
    if m:
        return m["name"].strip(), m["qty"].strip()
    m = re.match(r"^(?P<qty>\d+(?:\s+pack)?)\s+(?P<name>.+)$", text)       # "5 cashews for gravy", "1 pack hung..."
    if m:
        return m["name"].strip(), m["qty"].strip()
    return text.strip(), None


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def groceries() -> dict:
    weeks = []
    for wk, cats in _GROCERIES.items():
        categories = []
        seen = set()
        for cat, items in cats:
            out = []
            for text in items:
                name, qty = _split(text)
                ident = f"w{wk}-{_slug(name)}"
                n = 2
                while ident in seen:
                    ident, n = f"w{wk}-{_slug(name)}-{n}", n + 1
                seen.add(ident)
                out.append({"id": ident, "text": text, "name": name[0].upper() + name[1:], "quantity": qty,
                            "grocery_category": _GROCERY_CATS[cat]})
            categories.append({"category": cat, "items": out})
        weeks.append({"week": wk, "categories": categories})
    return {
        "note": ("Quantities are for both people for one week and include Person 1's extra portions. Buy fresh "
                 "vegetables twice a week. Staples like oil, salt and whole spices are assumed to be in the kitchen; a "
                 "master spice list is at the end. Adjust up/down slightly to taste."),
        "weeks": weeks,
        "pantry": ("Master spice & pantry list (buy once, refill as needed): salt, black salt (kala namak), turmeric, red "
                   "chilli powder, coriander powder, cumin seeds & powder, garam masala, chole masala, rajma masala, "
                   "sambar powder, mustard seeds, ajwain, kasuri methi, bay leaf, tamarind, soy sauce, baking-safe oil "
                   "(mustard/sunflower), papad, tea bag (chole colour)."),
        "pantry_hindi": "Ye masale ek baar le lo, mahine bhar chalega.",
    }


# Section 1 + Sections 4-6, kept as structured text (shown in a secondary "Targets & training" area).
CONTEXT = {
    "targets": {
        "method": ("Maintenance calories are estimated with the Mifflin–St Jeor equation and an activity factor for 4 "
                   "training days plus daily walking. A moderate calorie deficit is used so that fat is lost while "
                   "muscle is protected by high protein and hard training."),
        "rows": [
            ["Body stats", "32 yr • 178 cm • 85 kg", "32 yr • 160 cm • 70 kg"],
            ["BMR (resting burn)", "~1,808 kcal", "~1,379 kcal"],
            ["Maintenance (TDEE)", "~2,800 kcal", "~2,000 kcal"],
            ["Target (fat-loss)", "~2,300 kcal (≈500 deficit)", "~1,600 kcal (≈400 deficit)"],
            ["Protein target", "180 g/day (~2.1 g/kg)", "130 g/day (~1.85 g/kg)"],
            ["Carbohydrate target", "~235 g/day", "~145 g/day"],
            ["Fat target", "~70 g/day", "~55 g/day"],
            ["Fibre target", "30–38 g/day", "25–30 g/day"],
            ["Rate of loss (expected)", "0.4–0.6 kg/week", "0.3–0.5 kg/week"],
        ],
        "before_you_start": ("Weigh yourself once at the beginning and then weekly (same time, morning, after toilet, "
                             "before eating). Judge progress over 2–3 weeks, not day to day — water weight fluctuates. "
                             "If a woman is pregnant, breastfeeding, or anyone has a medical condition, check with a "
                             "doctor before starting a deficit or new training."),
    },
    "training": {
        "person1": {
            "title": "Person 1 (Male, Intermediate) — Upper / Lower (4 days)",
            "why": ("For an intermediate lifter who wants to keep strength while losing fat, an Upper/Lower split is the "
                    "best fit. Each muscle is trained twice a week, which is ideal for building/keeping muscle, and it's "
                    "simple to run in a normal gym."),
            "days": {"mon": "Upper A (Bench focus)", "tue": "Lower A (Squat focus)", "thu": "Upper B (Row / Overhead focus)",
                     "fri": "Lower B (Deadlift focus)"},
            "rest": "Wed, Sat, Sun off (or light walk)",
            "warmup": ("5 min easy cardio (bike/incline walk) → arm circles, leg swings, band pull-aparts 2×15 → then for "
                       "your first main lift do 2 light ramp-up sets: empty bar ×10, then ~50% of working weight ×5."),
            "warmup_hindi": "Pehle halka set lagao, phir working weight — chot nahi lagegi.",
            "rules": [
                "RPE = how hard a set feels. RPE 7 ≈ 3 reps left in the tank, RPE 8 ≈ 2 left, RPE 9 ≈ 1 left, RPE 10 = failure.",
                "Weekly intensity: Week 1 = RPE 7 (leave 3 reps), Week 2 = RPE 8, Week 3 = RPE 8.5–9 (hardest), Week 4 = Deload (~60% weight, half the sets).",
                "When to increase WEIGHT: if you hit the top of the rep range on all sets with good form and it still felt like the target RPE (or easier), add weight next time — +2.5 kg on upper-body lifts, +5 kg on squat/deadlift.",
                "When to increase REPS: if you can't add weight yet, keep the weight and add 1 rep per set until you reach the top of the range, then add weight.",
                "After failing a set (missed reps / form broke): rack it — don't grind ugly reps. Rest a bit longer, drop the weight ~10% and finish your remaining sets at target reps. Keep the same weight next session and try again. Only lower the program weight if you miss it 2 sessions in a row.",
                "Double progression rule: stay at a weight until you hit the top of the rep range on every set, then jump up.",
            ],
            "notation": "Sets × Reps @RPE. Tempo shown as e.g. 3-0-1 = 3 s down, 0 pause, 1 s up. Rest in minutes.",
            "week1": {
                "Upper A": [["Barbell Bench Press", "4 × 6–8 @7", "2.5 min", "3-0-1", "~47.5 kg"],
                            ["Seated Cable Row", "4 × 8–10 @7", "2 min", "2-1-1", "moderate"],
                            ["Incline Dumbbell Press", "3 × 8–10 @7", "2 min", "3-0-1", "moderate"],
                            ["Lat Pulldown", "3 × 10–12 @7", "90 s", "2-1-1", "moderate"],
                            ["Dumbbell Lateral Raise", "3 × 12–15 @7", "60 s", "2-0-1", "light"],
                            ["Triceps Rope Pushdown", "3 × 12–15 @7", "60 s", "2-0-1", "light"]],
                "Lower A": [["Barbell Back Squat", "4 × 6–8 @7", "3 min", "3-0-1", "~55 kg"],
                            ["Romanian Deadlift", "3 × 8–10 @7", "2.5 min", "3-1-1", "moderate"],
                            ["Leg Press", "3 × 10–12 @7", "2 min", "2-0-1", "moderate"],
                            ["Seated Leg Curl", "3 × 12–15 @7", "90 s", "2-1-1", "moderate"],
                            ["Standing Calf Raise", "4 × 12–15 @7", "60 s", "2-1-1", "moderate"]],
                "Upper B": [["Overhead Barbell Press", "4 × 6–8 @7", "2.5 min", "2-0-1", "moderate"],
                            ["Barbell Bent-Over Row", "4 × 8–10 @7", "2 min", "2-1-1", "moderate"],
                            ["Flat Dumbbell Press", "3 × 8–10 @7", "2 min", "3-0-1", "moderate"],
                            ["Chin-up (or Assisted)", "3 × 6–10 @7", "2 min", "2-0-1", "bodyweight"],
                            ["Face Pull", "3 × 15 @7", "60 s", "2-1-1", "light"],
                            ["Dumbbell Biceps Curl", "3 × 10–12 @7", "60 s", "2-0-1", "moderate"]],
                "Lower B": [["Conventional Deadlift", "4 × 4–6 @7", "3 min", "2-0-1", "~90 kg"],
                            ["Front Squat or Goblet Squat", "3 × 8–10 @7", "2.5 min", "3-0-1", "moderate"],
                            ["Walking Lunges", "3 × 10/leg @7", "2 min", "2-0-1", "light DBs"],
                            ["Leg Extension", "3 × 12–15 @7", "90 s", "2-1-1", "moderate"],
                            ["Hanging Knee Raise", "3 × 12–15 @7", "60 s", "2-0-1", "bodyweight"]],
            },
            "week1_columns": ["Exercise", "Sets × Reps", "Rest", "Tempo", "Start weight"],
            "weeks": {
                1: "Week 1 — RPE 7 (technique + base)",
                2: ("Week 2 — RPE 8 (add load). Same exercises and rep ranges. Push to RPE 8 (leave ~2 reps). Add +2.5 kg "
                    "to upper lifts and +5 kg to squat/deadlift wherever you hit the top of the range last week. Big lifts "
                    "approx: Bench ~50 kg, Squat ~60 kg, Deadlift ~95 kg, aiming for the same rep targets. Optional: add 1 "
                    "set to Bench and Squat if recovery is good."),
                3: ("Week 3 — RPE 8.5–9 (peak, hardest week). Heaviest, most demanding week. Reps live at the lower end of "
                    "each range but heavier. Leave only ~1 rep in the tank on top sets. Big lifts approx: Bench ~52.5 kg, "
                    "Squat ~65 kg, Deadlift ~100 kg (adjust to feel, not ego). Rest an extra 30–60 s on main lifts. If a "
                    "set drops below the min reps, that's your ceiling — hold it, don't force."),
                4: ("Week 4 — DELOAD (recover & grow). Use ~60% of your Week-3 weights, drop to 2 sets per exercise, stop "
                    "every set at RPE 5–6 (easy). Skip the smallest isolation moves (lateral raise, face pull, calf, curl, "
                    "ab work if tired). Keep protein and steps high. Goal: move well, feel fresh by Sunday."),
            },
        },
        "person2": {
            "title": "Person 2 (Female, Beginner) — Upper / Lower (4 days, beginner-friendly)",
            "why": ("Same simple Upper/Lower layout, but with machines and easy-to-learn movements first. The focus for "
                    "Weeks 1–2 is learning good form with light weight, then adding load."),
            "days": {"mon": "Lower A", "tue": "Upper A", "thu": "Lower B", "fri": "Upper B"},
            "rest": "Wed, Sat, Sun off (walk/yoga)",
            "warmup": ("5 min brisk walk/bike → 10 bodyweight squats, 10 glute bridges, arm circles, band pull-aparts "
                       "2×15. On your first machine, do 1 light set of 12 to groove the movement."),
            "warmup_hindi": "Pehle bina weight ke movement practice karo.",
            "rules": [
                "RPE feel: Week 1 = RPE 6–7 (very doable, 3–4 reps left), Week 2 = RPE 7, Week 3 = RPE 8 (2 left), Week 4 = Deload (easy).",
                "Increase reps first: start at the bottom of the range; each session try to add 1–2 reps until you reach the top of the range on all sets.",
                "Then increase weight: once you hit the top of the range on all sets with good form, add the smallest jump — +1–2.5 kg (machines/dumbbells) or the next pin — and drop back to the bottom of the range.",
                "After failing a set: no problem — rack it, rest, lower the weight a little, and finish your reps. Repeat the same weight next time.",
                "Form beats weight. A clean rep with lighter weight grows more muscle than a sloppy heavy one.",
            ],
            "week1": {
                "Lower A": [["Goblet Squat (dumbbell)", "3 × 10–12 @6–7", "90 s", "Chest up, sit back, knees track over toes"],
                            ["Leg Press (machine)", "3 × 12 @6–7", "90 s", "Don't lock knees hard at top"],
                            ["Glute Bridge / Hip Thrust", "3 × 12–15 @6–7", "75 s", "Squeeze glutes at top, ribs down"],
                            ["Seated Leg Curl", "3 × 12–15 @6–7", "75 s", "Slow and controlled"],
                            ["Standing Calf Raise", "3 × 15 @6–7", "45 s", "Full stretch at bottom"]],
                "Upper A": [["Lat Pulldown", "3 × 10–12 @6–7", "90 s", "Pull elbows down, don't lean back much"],
                            ["Chest Press (machine)", "3 × 10–12 @6–7", "90 s", "Shoulders back, smooth press"],
                            ["Seated Cable Row", "3 × 12 @6–7", "75 s", "Squeeze shoulder blades"],
                            ["Dumbbell Shoulder Press", "2 × 12 @6–7", "75 s", "Don't arch the back"],
                            ["Dumbbell Lateral Raise", "2 × 15 @6–7", "45 s", "Light weight, lead with elbows"]],
                "Lower B": [["Leg Press (higher reps)", "3 × 15 @6–7", "90 s", "Feet shoulder-width"],
                            ["Dumbbell Romanian Deadlift", "3 × 10–12 @6–7", "90 s", "Slight knee bend, push hips back, flat back"],
                            ["Walking Lunges (bodyweight)", "2 × 10/leg @6–7", "75 s", "Big step, torso tall"],
                            ["Cable/Machine Glute Kickback", "3 × 12/leg @6–7", "60 s", "Controlled squeeze"],
                            ["Plank", "3 × 20–30 s", "45 s", "Straight line, brace core"]],
                "Upper B": [["Assisted Pull-up (machine)", "3 × 8–10 @6–7", "90 s", "Full hang to chin over bar"],
                            ["Incline Dumbbell Press", "3 × 10–12 @6–7", "90 s", "Elbows ~45°"],
                            ["Machine Row", "3 × 12 @6–7", "75 s", "Chest on pad, pull to ribs"],
                            ["Face Pull", "3 × 15 @6–7", "60 s", "Pull to forehead, elbows high"],
                            ["Dumbbell Biceps Curl", "2 × 12 @6–7", "45 s", "No swinging"]],
            },
            "week1_columns": ["Exercise", "Sets × Reps", "Rest", "Form tip"],
            "weeks": {
                1: "Week 1 — RPE 6–7 (learn the movements)",
                2: ("Week 2 — RPE 7 (add a little). Same workouts. Add 1–2 reps per set where you can; when you reach the "
                    "top of a range on all sets, add the smallest weight jump (+1–2.5 kg or next pin). Sets stay the same. "
                    "Keep tempo controlled — still learning."),
                3: ("Week 3 — RPE 8 (challenge week). Leave about 2 reps in the tank on your last set of each exercise. Add "
                    "the +1 set option to Goblet Squat, Chest Press and Lat Pulldown if you feel good. If a set feels "
                    "wobbly, that's your top weight for now."),
                4: ("Week 4 — DELOAD (recover). Use ~60% of Week-3 weights, 2 sets per exercise, stop well short of hard "
                    "(RPE 5–6). Drop the small isolation moves (lateral raise, face pull, curls, plank) if tired. Focus on "
                    "smooth form, walking and sleep."),
            },
        },
        "cardio": ("You don't need much. On 1–2 rest days, do 20–30 min of easy incline walking or cycling. The daily step "
                   "goal already covers most of the fat-loss cardio. Keep intense cardio low so it doesn't eat into gym "
                   "recovery."),
    },
    "recovery": {
        "sleep": ["Aim for 7–9 hours a night, at a consistent time.", "Fixed sleep and wake time, even on weekends (±30 min).",
                  "No screens 30–45 min before bed; dim the lights.", "Last big meal 2–3 hours before bed; caffeine before 3 pm only.",
                  "Cool, dark, quiet room. A short 10–20 min nap is fine if nights are short."],
        "hydration": ["Person 1: ~3.5–4 litres water/day. Person 2: ~2.5–3 litres/day. Add ~500 ml for every hour of training or hot weather.",
                      "Drink 500 ml on waking and 500 ml around the workout (before + during).",
                      "Urine should be pale yellow. Dark = drink more.",
                      "On heavy sweat days, a pinch of salt + lemon in water (or coconut water) replaces electrolytes. Limit sugary drinks and juices — they add hidden calories."],
        "steps": {"person1": ["8,000 steps/day (Weeks 1–2)", "10,000 steps/day (Weeks 3–4)"],
                  "person2": ["7,000 steps/day (Weeks 1–2)", "8,000–9,000 steps/day (Weeks 3–4)"],
                  "note": "Post-dinner 15–20 min walks also help digestion and blood sugar.", "hindi": "Khaane ke baad walk zaroor karo."},
        "stretching": ["Chest/doorway stretch — 30 s each side", "Hamstring stretch (seated reach) — 30 s each leg",
                       "Hip-flexor lunge stretch — 30 s each side", "Quad stretch (standing) — 30 s each",
                       "Child's pose + cat-cow — 1 min", "Neck & shoulder rolls — 30 s"],
        "mobility": ["90/90 hip rotations — 8 each side", "Deep bodyweight squat hold — 3 × 30 s", "Ankle rocks against wall — 10 each",
                     "Thoracic (upper-back) rotations — 8 each side", "Band shoulder dislocates / pass-throughs — 10 reps",
                     "Glute bridges — 15 (also a good warm-up)"],
        "tips": ["Take rest days seriously — muscle grows on off days, not just in the gym.",
                 "Hit your protein every day; it's the #1 recovery tool alongside sleep.",
                 "Manage stress (10 min breathing/walk) — high stress slows fat loss and recovery.",
                 "If a joint hurts (sharp pain, not normal soreness), swap the exercise and don't push through.",
                 "Deload weeks (Week 4) are non-negotiable — they prevent burnout and injury.",
                 "Normal muscle soreness for 1–2 days is fine; light movement helps it fade."],
    },
    "supplements": {
        "intro": "Supplements are a small bonus on top of food, never a replacement. Only these have strong evidence and are worth considering:",
        "rows": [["Whey protein (veg-friendly)", "1 scoop (~24 g P) as needed to hit protein target",
                  "Convenience only — helps reach 130–180 g protein. Skip if you already hit it from food. Vegans can use soy/pea protein."],
                 ["Creatine monohydrate", "3–5 g every day (any time)",
                  "Best-studied supplement for strength & muscle; safe long-term. Especially useful for vegetarians. Drink extra water."],
                 ["Vitamin D3", "1,000–2,000 IU/day (check blood level)",
                  "Common deficiency in India, esp. indoors; supports bone/muscle. Ideally test first."],
                 ["Vitamin B12", "As per label / doctor", "Often low on vegetarian diets — worth monitoring and supplementing if low."],
                 ["Caffeine (coffee)", "~100–200 mg pre-workout (optional)", "Mild performance and focus boost. Not after 3 pm."]],
        "outro": ("Everything else (fat burners, BCAAs, test boosters) is unnecessary here. Consult a doctor before starting "
                  "supplements, particularly Vitamin D dosing."),
    },
    "meal_prep": [
        "Sunday batch (60–90 min): soak & pressure-cook the week's beans (rajma, chole, kala chana, lobia) and freeze in meal-size boxes; boil and squeeze soya chunks; roast peanuts; make a jar of mint-coriander chutney.",
        "Twice-weekly: chop onions, tomatoes, ginger-garlic paste and store airtight; wash and store greens. Cut prep time to minutes on busy days.",
        "Grains ahead: cook a batch of brown rice/quinoa; refrigerate 3–4 days. Knead atta fresh or store 2 days.",
        "Curd & sprouts: set curd every 2 days; start a sprout jar so a batch is always ready.",
        "Portion as you cook: plate Person 2's base serving first, then add Person 1's extras to the pan/plate — keeps macros on track without weighing every day.",
        "Keep protein visible: pre-portion whey scoops, keep roasted chana/peanuts handy so hitting the protein target is easy.",
    ],
    "meal_prep_hindi": "Ek din pehle taiyari karo — poora hafta easy ho jaata hai.",
    "monthly_adjust": ("If weight isn't moving after 2–3 weeks and steps + protein are on point, drop ~150 kcal (e.g. one "
                       "less roti/rice serving). If you feel weak or gym numbers crash, you may be cutting too hard — add "
                       "~150 kcal back. Re-check body weight and lifts at the end of the month and set up the next block."),
}

# Extraction artefacts fixed by hand (documented, nothing else is edited).
RECIPE_FIXES: dict = {}
