"""AURA Home redesign: views, widget visibility/order, status capsules, protection/system/assistant tiles."""

import pytest

from fixtures import calendar_payload, commute_payload, weather_payload

pytestmark = pytest.mark.browser

IS_OPEN = "document.getElementById('detail').open"
VIEW = "document.getElementById('detail').dataset.view"
VISIBLE = "[...document.querySelectorAll('#tiles [data-tile]')].filter(t => !t.hidden).map(t => t.dataset.tile)"

DNS = {"installed": True, "service": "running", "dns_ok": True, "blocking_ok": True,
       "last_ok_at": "2026-10-23T00:50:00Z", "checked_at": "2026-10-23T00:50:00Z", "stats": None,
       "stats_note": "Query statistics need a Pi-hole app password", "router_dns": ["192.168.1.11"],
       "router_uses_pihole": True, "pi_address": "192.168.1.11", "problems": []}


@pytest.fixture
def srv(make_server):
    return make_server(AURA_EDIT_ACCESS="lan")


def dashboard(chrome, srv, w=1920, h=1080, dns=DNS, storage_room=None):
    p = chrome.new_page()
    p.viewport(w, h)
    p.fixture("/api/weather", weather_payload(63, 12))
    p.fixture("/api/commute", commute_payload("active"))
    p.fixture("/api/calendar", calendar_payload())
    p.fixture("/api/system/dns-filter", dns)
    p.goto(f"{srv.url}/")
    if storage_room:
        p.js(f"sessionStorage.setItem('aura.view', '{storage_room}'); location.reload()")
    p.wait_for("document.querySelectorAll('.view-tab').length >= 1"
               " && document.getElementById('protection-tile').dataset.state !== 'loading'"
               " && document.getElementById('system-tile').dataset.state !== 'loading'"
               " && document.getElementById('weather').dataset.state === 'ok'")
    return p


def test_view_tabs_show_each_views_widgets(chrome, srv):
    p = dashboard(chrome, srv)
    tabs = p.js("[...document.querySelectorAll('.view-tab')].map(t => [t.textContent, t.getAttribute('aria-selected')])")
    assert tabs == [["Home", "true"], ["Daily planning", "false"], ["Smart home", "false"], ["System", "false"]]
    assert p.js(VISIBLE) == ["weather", "commute", "meals", "advice", "calendar", "groceries", "assistant", "smarthome"]
    p.js("document.querySelector('.view-tab[data-view=daily]').click()")
    assert sorted(p.js(VISIBLE)) == ["advice", "calendar", "commute", "groceries", "meals"]
    assert p.js("document.getElementById('calendar-tile').dataset.size") == "l"
    p.js("document.querySelector('.view-tab[data-view=system]').click()")
    assert sorted(p.js(VISIBLE)) == ["assistant", "protection", "smarthome", "system"]
    assert p.js("document.querySelector('.view-tab[data-view=system]').getAttribute('aria-selected')") == "true"
    assert p.js("sessionStorage.getItem('aura.view')") == "system"           # this screen, this session
    assert p.js("document.querySelector('.views__edit').getAttribute('href')") == "/settings#views"
    assert p.errors == []


def test_switching_views_makes_no_requests(chrome, srv):
    p = dashboard(chrome, srv)
    p.js("""window.__reqs = 0; const f = window.fetch; window.fetch = (...a) => { window.__reqs++; return f(...a); };""")
    for v in ("daily", "smart-home", "system", "home"):
        p.js(f"document.querySelector('.view-tab[data-view={v}]').click()")
    assert p.js("window.__reqs") == 0


def test_view_tabs_keyboard_and_number_keys(chrome, srv):
    p = dashboard(chrome, srv)
    p.call("Page.bringToFront")               # key events go to the front tab
    p.js("document.querySelector('.view-tab').focus()")
    p.wait_for("document.activeElement.classList.contains('view-tab')", timeout=3)
    p.call("Input.dispatchKeyEvent", type="keyDown", key="ArrowRight", code="ArrowRight", windowsVirtualKeyCode=39)
    p.call("Input.dispatchKeyEvent", type="keyUp", key="ArrowRight", code="ArrowRight", windowsVirtualKeyCode=39)
    p.wait_for("document.activeElement.dataset.view === 'daily'", timeout=3)
    p.call("Input.dispatchKeyEvent", type="keyDown", key="End", code="End", windowsVirtualKeyCode=35)
    p.wait_for("document.activeElement.dataset.view === 'system'", timeout=3)
    p.call("Input.dispatchKeyEvent", type="keyDown", key="3", code="Digit3", text="3", windowsVirtualKeyCode=51)
    p.wait_for("document.querySelector('.view-tab[aria-selected=true]').dataset.view === 'smart-home'", timeout=3)


def test_custom_views_hidden_widgets_and_default_view(chrome, srv):
    status, body = srv.api("PATCH", "/api/settings/home", {"name": "Sharma home", "hidden_tiles": ["system"]})
    assert status == 200, body
    status, body = srv.api("PATCH", "/api/settings/views", {"default_view": "living-room", "views": [
        {"id": "living-room", "name": "Living room", "columns": "2", "tiles": [{"id": "groceries", "size": "w"}, "weather", "system"]},
        {"id": "bedroom", "name": "Bedroom", "tiles": []}]})
    assert status == 200, body
    p = dashboard(chrome, srv)
    assert p.text("#home-name") == "Sharma home"
    assert p.js("document.querySelector('.view-tab[aria-selected=true]').dataset.view") == "living-room"   # the default view
    assert p.js(VISIBLE) == ["weather", "groceries"]                       # hidden everywhere stays hidden
    first = p.js("[...document.querySelectorAll('#tiles [data-tile]:not([hidden])')].sort((a, b) => a.style.order - b.style.order)[0].dataset.tile")
    assert first == "groceries" and p.js("document.getElementById('tiles').dataset.cols") == "2"
    p.js("document.querySelector('.view-tab[data-view=bedroom]').click()")
    assert p.js(VISIBLE) == [] and "Nothing in Bedroom yet" in p.text("#tiles-empty")


def test_unknown_saved_view_falls_back_to_the_default(chrome, srv):
    p = dashboard(chrome, srv, storage_room="garage")
    assert p.js("document.querySelector('.view-tab[aria-selected=true]').dataset.view") == "home"


def test_status_capsules_come_from_real_data_and_open_details(chrome, srv, artifacts):
    srv.api("POST", "/api/groceries", {"name": "Milk"})
    p = dashboard(chrome, srv)
    p.wait_for("document.querySelectorAll('.status__btn').length >= 5")
    caps = p.js("[...document.querySelectorAll('.status__btn')].map(b => [b.dataset.open, b.textContent.replace(/\\s+/g, ' ').trim()])")
    by = dict(caps)
    assert "29° Rain" in by["weather"]
    assert "Leave 9:15 AM · 24 min" in by["commute"]
    assert "Rohan's birthday" in by["calendar"]
    assert "1 to buy" in by["groceries"]
    assert "On for home network" in by["protection"]
    p.js("document.querySelector('.status__btn[data-open=commute]').click()")
    p.wait_for(f"{IS_OPEN} && {VIEW} === 'commute'")
    p.screenshot(str(artifacts / "home-capsule-commute.png"))


def test_no_capsule_without_data(chrome, srv):
    p = chrome.new_page()
    p.viewport(1920, 1080)
    p.fixture("/api/commute", commute_payload("unconfigured"))
    p.fixture("/api/calendar", {**calendar_payload(), "status": "unconfigured", "days": [], "feeds": []})
    p.fixture("/api/system/dns-filter", {**DNS, "installed": False, "service": "not_installed"})
    p.goto(f"{srv.url}/")
    p.wait_for("document.getElementById('protection-tile').dataset.state !== 'loading'"
               " && document.getElementById('commute').dataset.state !== 'loading'")
    opens = p.js("[...document.querySelectorAll('.status__btn')].map(b => b.dataset.open)")
    for absent in ("commute", "calendar", "groceries", "protection"):
        assert absent not in opens                                            # nothing invented
    assert p.text("#protection-status") == "Not installed"


@pytest.mark.parametrize("dns, value, words", [
    (DNS, "Active", "Router gives out this Pi"),
    ({**DNS, "router_dns": ["192.168.1.1"], "router_uses_pihole": False}, "Running", "Not network-wide"),
    ({**DNS, "router_dns": [], "router_uses_pihole": None}, "Running", "couldn't tell"),
    ({**DNS, "service": "stopped", "dns_ok": False, "problems": ["The Pi-hole service isn't running."]}, "Stopped", "isn't running"),
    ({**DNS, "blocking_ok": False, "problems": ["A known ad domain was not blocked"]}, "Needs attention", "not blocked"),
])
def test_protection_tile_never_overclaims(chrome, srv, dns, value, words):
    p = dashboard(chrome, srv, dns=dns)
    assert p.text("#protection-status") == value
    assert words in p.text("#protection-sub")
    p.js("document.querySelector('#protection-tile .tile-arrow').click()")
    p.wait_for(f"{IS_OPEN} && {VIEW} === 'protection'")
    body = p.text("#detail-body")
    assert "Service health" in body and "Which devices are protected" in body
    assert "blocked" not in body.lower() or dns.get("stats") or "Not blocking" in body or "not blocked" in body


def test_system_tile_and_detail_show_real_measurements(chrome, srv):
    p = dashboard(chrome, srv)
    text = p.text("#system-stats")
    assert "°C" in text and "% used" in text and "GB free" in text
    p.js("document.querySelector('#system-tile .tile-arrow').click()")
    p.wait_for(f"{IS_OPEN} && {VIEW} === 'system'")
    body = p.text("#detail-body")
    for k in ("CPU temperature", "Memory", "Storage", "Uptime", "Running since"):
        assert k in body, k


def test_assistant_tile_reflects_real_state(chrome, srv):
    p = dashboard(chrome, srv)
    p.wait_for("document.getElementById('assistant-status').textContent.trim() !== ''")
    # voice worker isn't running in tests: say so, never "Listening"
    assert p.text("#assistant-status") in ("Voice unavailable", "Type a question")
    assert p.js("document.getElementById('assistant-tile').dataset.state") != "listening"
    p.js("window.AuraAssistant.onEvent({state: 'listening'})")
    assert p.text("#assistant-status") == "Listening…"
    p.js("window.AuraAssistant.onEvent({state: 'idle'})")
    assert p.text("#assistant-status") != "Listening…"


def test_settings_refresh_keeps_keyboard_focus_on_the_tabs(chrome, srv):
    p = dashboard(chrome, srv)
    p.js("document.querySelector('.view-tab[data-view=daily]').focus()")
    srv.api("PATCH", "/api/settings/display", {"clock_format": "24h"})     # screens re-read their settings
    p.wait_for("/^\\d{2}:\\d{2}/.test(document.getElementById('clock').textContent)", timeout=10)   # the refresh happened
    assert p.js("document.activeElement.dataset.view") == "daily"
