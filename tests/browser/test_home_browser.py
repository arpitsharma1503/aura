"""AURA Home redesign: rooms, tile visibility/order, status capsules, protection/system/assistant tiles."""

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
        p.js(f"localStorage.setItem('aura.room', '{storage_room}'); location.reload()")
    p.wait_for("document.querySelectorAll('.room-tab').length >= 1"
               " && document.getElementById('protection-tile').dataset.state !== 'loading'"
               " && document.getElementById('system-tile').dataset.state !== 'loading'"
               " && document.getElementById('weather').dataset.state === 'ok'")
    return p


def test_room_tabs_filter_tiles(chrome, srv):
    p = dashboard(chrome, srv)
    tabs = p.js("[...document.querySelectorAll('.room-tab')].map(t => [t.textContent, t.getAttribute('aria-selected')])")
    assert tabs == [["Overview", "true"], ["Entrance", "false"], ["Kitchen", "false"], ["Network & system", "false"]]
    assert len(p.js(VISIBLE)) == 8
    p.js("document.querySelector('.room-tab[data-room=kitchen]').click()")
    assert p.js(VISIBLE) == ["groceries"]
    p.js("document.querySelector('.room-tab[data-room=network]').click()")
    assert sorted(p.js(VISIBLE)) == ["protection", "system"]
    assert p.js("document.querySelector('.room-tab[data-room=network]').getAttribute('aria-selected')") == "true"
    assert p.js("localStorage.getItem('aura.room')") == "network"           # remembered on this screen only
    assert p.errors == []


def test_room_tabs_keyboard(chrome, srv):
    p = dashboard(chrome, srv)
    p.js("document.querySelector('.room-tab').focus()")
    p.call("Input.dispatchKeyEvent", type="keyDown", key="ArrowRight", code="ArrowRight", windowsVirtualKeyCode=39)
    p.call("Input.dispatchKeyEvent", type="keyUp", key="ArrowRight", code="ArrowRight", windowsVirtualKeyCode=39)
    assert p.js("document.activeElement.dataset.room") == "entrance"
    assert sorted(p.js(VISIBLE)) == sorted(["weather", "advice", "commute", "calendar", "assistant"])
    p.call("Input.dispatchKeyEvent", type="keyDown", key="End", code="End", windowsVirtualKeyCode=35)
    assert p.js("document.activeElement.dataset.room") == "network"


def test_hidden_tiles_order_and_custom_room(chrome, srv):
    status, body = srv.api("PATCH", "/api/settings/home", {
        "name": "Sharma home", "tile_order": ["groceries", "weather"], "hidden_tiles": ["system"],
        "rooms": [{"id": "living-room", "name": "Living room", "tiles": ["weather", "calendar", "system"]},
                  {"id": "bedroom", "name": "Bedroom", "tiles": []}]})
    assert status == 200, body
    p = dashboard(chrome, srv)
    assert p.text("#home-name") == "Sharma home"
    visible = p.js(VISIBLE)
    assert "system" not in visible and len(visible) == 7
    first = p.js("[...document.querySelectorAll('#tiles [data-tile]:not([hidden])')].sort((a, b) => a.style.order - b.style.order)[0].dataset.tile")
    assert first == "groceries"
    p.js("document.querySelector('.room-tab[data-room=living-room]').click()")
    assert sorted(p.js(VISIBLE)) == ["calendar", "weather"]                 # hidden tiles stay hidden in rooms too
    p.js("document.querySelector('.room-tab[data-room=bedroom]').click()")
    assert p.js(VISIBLE) == [] and "Nothing in Bedroom yet" in p.text("#tiles-empty")


def test_unknown_saved_room_falls_back_to_overview(chrome, srv):
    p = dashboard(chrome, srv, storage_room="garage")
    assert p.js("document.querySelector('.room-tab[aria-selected=true]').dataset.room") == "overview"


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


def test_settings_home_editor_round_trip(chrome, srv, artifacts):
    p = chrome.new_page()
    p.viewport(1440, 900)
    p.goto(f"{srv.url}/settings#home")
    p.wait_for("!document.getElementById('s-main').hidden && document.querySelectorAll('#room-editor > li').length === 3", timeout=15)
    # hide System, move Groceries to the top, add a room with two tiles, rename Kitchen
    p.js("document.querySelector('#tile-editor li[data-value=system] input').click()")
    for _ in range(3):
        p.js("document.querySelector('#tile-editor li[data-value=groceries] .s-move').click()")
    p.js("document.getElementById('room-new').value = 'Living room'; document.getElementById('room-add').click()")
    p.js("""(() => { const li = document.querySelector('#room-editor li[data-id=living-room]');
        li.querySelector('input[value=weather]').click(); li.querySelector('input[value=calendar]').click(); })()""")
    p.js("""(() => { const n = document.querySelector('#room-editor li[data-id=kitchen] .s-room__name');
        n.value = 'Kitchen & dining'; n.dispatchEvent(new Event('input', {bubbles: true})); })()""")
    assert not p.js("document.querySelector('#home .s-save').disabled")
    p.screenshot(str(artifacts / "settings-home-editor.png"))
    p.js("document.querySelector('#home .s-save').click()")
    p.wait_for("document.querySelector('#home .s-status').dataset.kind === 'ok'")
    status, body = srv.api("GET", "/api/config")
    home = body["home"]
    assert home["tile_order"][0] == "groceries" and home["hidden_tiles"] == ["system"]
    assert {"id": "living-room", "name": "Living room", "tiles": ["weather", "calendar"]} in home["rooms"]
    assert any(r["name"] == "Kitchen & dining" for r in home["rooms"])
    # duplicate room names are refused in the editor
    p.js("document.getElementById('room-new').value = 'living ROOM'; document.getElementById('room-add').click()")
    assert "already a room" in p.text("#home .s-status")
    assert p.errors == []
