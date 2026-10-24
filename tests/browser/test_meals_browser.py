"""Meal plan in the browser: tile, detail view, persistence, groceries, midnight rollover."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.nutrition import load_plan, plan_day
from fixtures import calendar_payload, commute_payload, weather_payload

pytestmark = pytest.mark.browser
IS_OPEN = "document.getElementById('detail').open"
DNS = {"installed": False, "service": "not_installed", "dns_ok": None, "blocking_ok": None, "last_ok_at": None,
       "checked_at": "2026-10-23T00:50:00Z", "stats": None, "stats_note": None, "router_dns": [],
       "router_uses_pihole": None, "pi_address": None, "problems": []}


@pytest.fixture
def srv(make_server):
    return make_server(AURA_EDIT_ACCESS="lan")


def dashboard(chrome, srv, w=1920, h=1080, meals_day=None):
    p = chrome.new_page()
    p.viewport(w, h)
    p.fixture("/api/weather", weather_payload(63, 12))
    p.fixture("/api/commute", commute_payload("active"))
    p.fixture("/api/calendar", calendar_payload())
    p.fixture("/api/system/dns-filter", DNS)
    if meals_day:
        p.fixture("/api/meals/day", meals_day)
    p.goto(f"{srv.url}/")
    p.wait_for("document.getElementById('meals-tile').dataset.state === 'ok'")
    return p


def open_view(p):
    p.js("document.querySelector('#meals-tile .tile-arrow').click()")
    p.wait_for(f"{IS_OPEN} && !!document.querySelector('.mp-meal')", timeout=15)


def test_tile_shows_todays_plan_day_from_the_pdf(chrome, srv):
    p = dashboard(chrome, srv)
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    entry = load_plan()["schedule"][plan_day(today) - 1]
    assert p.text("#meals-day").startswith(f"Day {plan_day(today)} ·")
    text = p.text("#meals-list")
    for meal in ("breakfast", "lunch", "dinner"):
        assert entry[meal]["label"] in text
    assert "Tonight:" in p.text("#meals-prep")
    assert p.errors == []


def test_detail_person_switch_and_eaten_ticks_persist(chrome, srv, artifacts):
    p = dashboard(chrome, srv)
    open_view(p)
    assert p.js("document.getElementById('detail').dataset.view") == "meals"
    p.js("document.querySelector('[data-person=person2]').click()")
    first = p.text(".mp-meal")
    assert "Person 2: base plate" in first and "kcal" in first and "Person 1" not in first.split("Base plate")[0]
    p.js("document.querySelector('[data-person=person1]').click()")
    assert "Person 1: base plate +" in p.text(".mp-meal")
    p.js("document.querySelector('.mp-meal .mp-check').click()")
    p.wait_for("document.querySelector('.mp-meal .mp-check').getAttribute('aria-pressed') === 'true'")
    status, body = srv.api("GET", "/api/meals/day")
    assert body["eaten"]["breakfast"]["person1"] is True
    p.screenshot(str(artifacts / "meals-detail-person1.png"))
    p2 = dashboard(chrome, srv)                      # reload: still ticked, tile shows partial progress
    assert p2.js("document.querySelector('.meals__item').dataset.done") == "some"


def test_prepare_for_tomorrow_ticks_persist(chrome, srv):
    p = dashboard(chrome, srv)
    open_view(p)
    assert "Prepare for tomorrow" in p.text("#detail-body").title() or "PREPARE FOR TOMORROW" in p.text("#detail-body").upper()
    box = "document.querySelector('.mp-task input')"
    p.js(f"{box}.click()")
    p.wait_for(f"{box}.checked && document.querySelector('.mp-task').classList.contains('is-done')")
    status, body = srv.api("GET", "/api/meals/day")
    assert any(t["done"] for t in body["tomorrow"]["tasks"])


def test_recipe_page_scales_and_shows_both_portions(chrome, srv, artifacts):
    p = dashboard(chrome, srv)
    open_view(p)
    p.js("document.querySelector('.mp-meal .mp-meal__open').click()")
    p.wait_for("!!document.querySelector('.mp-rtitle')")
    text = p.text("#detail-body")
    for k in ("Per base serving", "Ingredients", "Method", "base + extras", "Preparation time isn't given in the plan"):
        assert k in text, k
    p.js("document.querySelectorAll('.mp-step')[1].click()")
    p.wait_for("document.querySelector('.mp-step__val').textContent.startsWith('3')")
    assert "Scaled ×1.5" in p.text("#detail-body") and "plan:" in p.text(".mp-ings")
    p.js("document.querySelector('.mp-back').click()")
    p.wait_for("!!document.querySelector('.mp-meal') && !document.querySelector('.mp-rtitle')")


def test_groceries_add_is_idempotent(chrome, srv, artifacts):
    p = dashboard(chrome, srv)
    open_view(p)
    p.js("document.querySelector('[data-tab=groceries]').click()")
    p.wait_for("!!document.querySelector('.mp-add')", timeout=15)
    name = p.js("document.querySelector('.mp-gitem label span').firstChild.textContent")
    p.js("document.querySelector('.mp-add').click()")
    p.wait_for(f"document.querySelector('.mp-gitem').textContent.includes('On list')")
    status, body = srv.api("GET", "/api/groceries")
    assert sum(1 for i in body["items"] if i["name"] == name) == 1
    status, again = srv.api("POST", "/api/meals/groceries/add", {"items": [{"name": name}]})
    assert again["added"] == [] and again["skipped"] == [name]
    p.screenshot(str(artifacts / "meals-groceries-added.png"))


def test_plan_tab_and_names_from_settings(chrome, srv):
    srv.api("PATCH", "/api/settings/meals", {"person1_name": "Arpit", "person2_name": "Riya"})
    p = dashboard(chrome, srv)
    open_view(p)
    assert p.text("[data-person=person1]") == "Arpit" and "Riya" in p.text("#detail-body")
    p.js("document.querySelector('[data-tab=plan]').click()")
    p.wait_for("!!document.querySelector('.mp-month')", timeout=15)
    text = p.text("#detail-body")
    for k in ("Targets", "The 30 days", "Training · Arpit", "Training · Riya", "Supplements", "Recovery & hydration"):
        assert k in text, k
    assert p.js("document.querySelectorAll('.mp-month li').length") == 30


def test_rolls_over_at_midnight_without_reload(chrome, srv):
    """If the screen's date is past the loaded plan date, it refetches by itself."""
    status, real = srv.api("GET", "/api/meals/day")
    stale = {**real, "today": "2000-01-01"}
    p = dashboard(chrome, srv, meals_day=stale)
    p.js("""window.__mealFetches = 0; const f = window.fetch;
            window.fetch = (u, o) => { if (String(u).includes('/api/meals/day')) window.__mealFetches++; return f(u, o); };""")
    p.wait_for("window.__mealFetches >= 1", timeout=5)
    assert p.js("location.pathname") == "/"            # no page reload


def test_turned_off_hides_the_tile(chrome, srv):
    srv.api("PATCH", "/api/settings/meals", {"enabled": False})
    p = chrome.new_page()
    p.viewport(1920, 1080)
    p.goto(f"{srv.url}/")
    p.wait_for("document.querySelectorAll('.room-tab').length > 0")
    assert p.js("document.getElementById('meals-tile').hidden")


def test_rest_of_month_button_adds_once(chrome, srv, artifacts):
    p = dashboard(chrome, srv)
    open_view(p)
    p.js("document.querySelector('[data-tab=groceries]').click()")
    p.wait_for("!!document.getElementById('mp-rest')", timeout=15)
    assert "Dry goods for the whole month on the 1st" in p.text("#dsec-auto-groceries")
    p.js("document.getElementById('mp-rest').click()")
    p.wait_for("!document.getElementById('mp-rest') && document.querySelector('#dsec-auto-groceries .mp-batches') !== null", timeout=15)
    assert "Rest of the month" in p.text("#dsec-auto-groceries")
    status, body = srv.api("GET", "/api/groceries")
    names = [i["name"].lower() for i in body["items"]]
    assert names and len(names) == len(set(names))
    p.screenshot(str(artifacts / "meals-auto-groceries.png"))
    assert p.errors == []


def test_add_message_survives_the_repaint(chrome, srv):
    p = dashboard(chrome, srv)
    open_view(p)
    p.js("document.querySelector('[data-tab=groceries]').click()")
    p.wait_for("!!document.querySelector('.mp-add')", timeout=15)
    p.js("document.querySelector('.mp-add').click()")
    p.wait_for("document.querySelector('.mp-gitem').textContent.includes('On list')")
    assert not p.js("document.querySelector('.mp-flash').hidden") and "Added 1" in p.text(".mp-flash")
