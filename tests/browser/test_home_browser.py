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
    assert tabs == [["Home", "true"], ["Daily planning", "false"], ["Smart home", "false"], ["Price Tracker", "false"], ["System", "false"]]
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
    """Calm pills: only alerts not already shown by a tile, and the internet speed. The Home
    tiles carry the same live facts (one each)."""
    srv.api("POST", "/api/groceries", {"name": "Milk"})
    stopped = {**DNS, "service": "stopped", "dns_ok": False, "problems": ["The Pi-hole service isn't running."]}
    p = dashboard(chrome, srv, dns=stopped)
    p.wait_for("[...document.querySelectorAll('.status__btn')].some(b => b.dataset.open === 'protection')")
    caps = p.js("[...document.querySelectorAll('.status__btn')].map(b => [b.dataset.open, b.dataset.kind || ''])")
    assert ["protection", "alert"] in caps                                  # ad blocking is off: an alert pill
    assert all(kind == "alert" or opens == "system" for opens, kind in caps), caps
    p.wait_for("document.querySelector('#commute .glance__big')?.textContent === 'Leave 9:15 AM'")
    assert p.text("#commute .glance__line").startswith("24 min to ")
    assert p.text("#grocery-tile .glance__big") == "1" and "Rohan's birthday" in p.text("#calendar-tile .glance__big")
    p.js("document.querySelector('.status__btn[data-open=protection]').click()")
    p.wait_for(f"{IS_OPEN} && {VIEW} === 'protection'")
    p.screenshot(str(artifacts / "home-capsule-protection.png"))


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


SPEED = {"status": "ok", "message": None, "down_mbps": 29.8, "ping_ms": 113, "measured_at": "2026-11-03T20:01:35+05:30",
         "failed_at": None, "every_minutes": 10, "source": "Cloudflare speed test"}


@pytest.mark.parametrize("speed, value, kind", [
    (SPEED, "↓ 29.8 Mbps · 113 ms", None),
    ({**SPEED, "down_mbps": 412.6}, "↓ 413 Mbps · 113 ms", None),
    ({**SPEED, "status": "offline", "failed_at": "2026-11-03T20:11:35+05:30"}, "No internet", "alert"),
    ({**SPEED, "status": "off", "down_mbps": None, "ping_ms": None, "measured_at": None}, None, None),
])
def test_internet_speed_capsule_comes_last(chrome, srv, artifacts, speed, value, kind):
    p = chrome.new_page()
    p.viewport(1920, 1080)
    p.fixture("/api/system/speed", speed)
    p.fixture("/api/system/dns-filter", DNS)
    p.goto(f"{srv.url}/")
    p.wait_for("document.getElementById('protection-tile').dataset.state !== 'loading'")
    labels = "[...document.querySelectorAll('.status__btn')].map(b => b.getAttribute('aria-label'))"
    if value is None:
        p.wait_for("performance.getEntriesByName(location.origin + '/api/system/speed').length > 0")
        assert not any(x.startswith("Internet") for x in p.js(labels))           # nothing invented
        return
    p.wait_for(f"{labels}.some(x => x.startsWith('Internet'))")
    caps = p.js("[...document.querySelectorAll('.status__btn')].map(b => [b.dataset.open, b.getAttribute('aria-label'), b.dataset.kind || null])")
    i = next(k for k, c in enumerate(caps) if c[1].startswith("Internet"))
    assert caps[i] == ["internet", f"Internet: {value}", kind]
    assert i == len(caps) - 1                                                       # after any alerts
    p.js("[...document.querySelectorAll('.status__btn')].find(b => b.textContent.includes('Internet')).click()")
    p.wait_for("document.getElementById('net-dialog').open")                       # the Internet sheet, not a detail
    assert not p.js(IS_OPEN)
    if speed is SPEED:
        p.screenshot(str(artifacts / "home-capsule-internet.png"))
    p.click("#net-more")                                                            # still one tap to System
    p.wait_for(f"{IS_OPEN} && {VIEW} === 'system'")


def test_internet_sheet_shows_quality_trend_and_runs_a_test(chrome, srv):
    hist = [{"at": f"2026-11-03T19:{m:02d}:00+05:30", "down_mbps": v, "ping_ms": 90 if v else None}
            for m, v in [(1, 12.0), (11, None), (21, 4.5), (31, 8.2)]]
    speed = {**SPEED, "down_mbps": 4.5, "ping_ms": 91, "checking": False, "history": hist,
             "quality": {"level": "slow", "label": "Slow", "detail": "Pages and video may load slowly."}}
    p = chrome.new_page()
    p.viewport(390, 844, mobile=True, scale=2)
    p.fixture("/api/system/speed/check", {**speed, "started": True, "checking": True})   # first: "/speed" would match it too
    p.fixture("/api/system/speed", speed)
    p.fixture("/api/system/network", {"available": True, "hostname": "aura", "lan_ip": "192.168.1.5", "wifi_connected": True,
                                      "wifi_ssid": "Home", "wifi_signal": 74, "wifi_freq_mhz": 2437, "wifi_rate_mbps": 130,
                                      "default_interface": "wlan0"})
    p.goto(f"{srv.url}/")
    p.wait_for("!!document.querySelector('.status__btn[data-open=internet]')")
    assert p.js("document.querySelector('.status__btn[data-open=internet]').dataset.level") == "slow"   # red dot
    p.click(".status__btn[data-open=internet]")
    p.wait_for("document.getElementById('net-dialog').open && document.getElementById('net-wifi').textContent.includes('GHz')")
    assert p.text("#net-down") == "4.5" and p.text("#net-badge") == "Slow" and p.text("#net-ping") == "91 ms"
    assert p.text("#net-wifi") == "2.4 GHz · 74%"
    assert p.js("!document.getElementById('net-tip').hidden")                     # 2.4 GHz and slow: the tip shows
    assert p.js("document.querySelectorAll('#net-spark .net-spark__miss').length") == 1   # the failed check
    assert p.js("!!document.querySelector('#net-spark .net-spark__line')")
    p.click("#net-test")
    p.wait_for("document.getElementById('net-test').disabled && document.getElementById('net-test').textContent === 'Testing…'")
    p.wait_for("document.getElementById('net-badge').textContent === 'Checking…'", timeout=5)
    assert p.js("document.querySelector('.status__btn[data-open=internet]').dataset.level") == "checking"


@pytest.mark.parametrize("w, h, mobile", [(1920, 1080, False), (390, 844, True)])
def test_home_app_look(chrome, srv, w, h, mobile):
    """The household asked for the look of Apple's Home app and its wall display: a large
    home-name title (also on phones), the selected view in white glass (sidebar row on TVs,
    tab bar on phones), big rounded glass tiles, an analog clock on TVs, and Inter (bundled)
    where San Francisco isn't installed."""
    p = chrome.new_page()
    p.viewport(w, h, mobile=mobile, scale=2 if mobile else 1)
    p.goto(f"{srv.url}/")
    p.wait_for("document.querySelectorAll('.view-tab').length >= 1 && document.fonts.status === 'loaded'")
    s = p.js("""(() => {
      const cs = (q) => getComputedStyle(document.querySelector(q));
      return { title: cs('.brand__home').display, titleSize: parseFloat(cs('.brand__home').fontSize),
        tab: cs('.view-tab[aria-selected=true]').backgroundColor, tabInk: cs('.view-tab[aria-selected=true]').color,
        other: cs('.view-tab[aria-selected=false]').backgroundColor, radius: parseFloat(cs('#weather').borderTopLeftRadius),
        icon: cs('#weather .tile__icon').borderTopLeftRadius, inter: document.fonts.check('16px Inter'),
        wide: document.documentElement.scrollWidth <= document.documentElement.clientWidth };
    })()""")
    assert s["title"] != "none" and s["titleSize"] >= 26
    assert s["tab"].startswith(("rgba(255, 255, 255", "rgb(255, 255, 255")) and s["tab"] != s["other"]
    assert s["radius"] >= 24 and s["inter"] and s["wide"]
    # Home on a TV is the wall-display grid: a clock tile with an analog face; phones keep the clock on top
    clock = p.js("getComputedStyle(document.getElementById('clock-tile')).display")
    assert (clock != "none") == (not mobile)
    if not mobile:
        assert p.js("document.querySelector('#clock-tile .aclock__m').getAttribute('transform')").startswith("rotate(")
        assert p.text("#ctile-time")[:1].isdigit()
        order = p.js("""[...document.querySelectorAll('#tiles .tile')].filter(t => t.getClientRects().length)
          .map(t => { const r = t.getBoundingClientRect(); return [t.dataset.tile || t.id, Math.round(r.left), Math.round(r.top)]; })""")
        named = {n: (x, y) for n, x, y in order}
        assert named["weather"][0] < named["clock-tile"][0] and named["clock-tile"][1] < named["calendar"][1]
        assert {"advice", "assistant"}.isdisjoint(named)


def test_calm_home_view_shows_one_fact_per_tile(chrome, srv):
    """The household found Home too busy: in the Home view each tile shows one big fact and a
    line (details are one tap away), and the pills show only alerts and the internet speed
    (on every view). Other views keep every detail."""
    srv.api("POST", "/api/groceries", {"name": "Milk"})
    srv.api("POST", "/api/groceries", {"name": "Eggs"})
    p = dashboard(chrome, srv)
    p.wait_for("document.querySelector('#grocery-tile .glance__big')?.textContent === '2'")
    vis = "(q) => { const e = document.querySelector(q); return !!e && getComputedStyle(e).display !== 'none' && e.getClientRects().length > 0; }"
    assert p.js(f"({vis})('#grocery-tile .glance') && !({vis})('#grocery-list') && !({vis})('#weather .daily')")
    assert p.text("#grocery-tile .glance__line") == "to buy"
    caps = p.js("[...document.querySelectorAll('.status__btn')].map(b => [b.dataset.open, b.dataset.kind || ''])")
    assert all(kind == "alert" or opens == "system" for opens, kind in caps)      # alerts and Internet only
    assert "groceries" not in [o for o, _ in caps]
    # Daily planning: the full tiles; the pills stay calm on every view
    p.js("document.querySelector('.view-tab[data-view=daily]').click()")
    p.wait_for(f"({vis})('#grocery-list') && !({vis})('#grocery-tile .glance')")
    assert "groceries" not in p.js("[...document.querySelectorAll('.status__btn')].map(b => b.dataset.open)")


def test_popups_on_phones_are_bottom_sheets_and_the_page_stays_put(chrome, srv):
    """Seen 16 Nov: on a phone the popup filled the screen and floated up while growing."""
    p = dashboard(chrome, srv)
    p.viewport(390, 844, mobile=True, scale=3)
    p.js("document.scrollingElement.scrollTop = 300")
    before = p.js("document.scrollingElement.scrollTop")
    p.js("window.AuraDetail.open('weather')")
    p.wait_for(f"{IS_OPEN} && getComputedStyle(document.getElementById('detail')).transform === 'none'"
               " || getComputedStyle(document.getElementById('detail')).transform === 'matrix(1, 0, 0, 1, 0, 0)'", timeout=3)
    r = p.js("(() => { const r = document.getElementById('detail').getBoundingClientRect(); return [r.top, r.bottom, innerHeight]; })()")
    assert 0 < r[0] < 60 and abs(r[1] - r[2]) <= 1                 # under the status bar, down to the bottom edge
    cs = p.js("(() => { const cs = getComputedStyle(document.getElementById('detail')); return [cs.position, cs.borderBottomLeftRadius, cs.animationName]; })()")
    assert cs == ["fixed", "0px", "aura-sheet-up"]                    # slides up from the bottom, no zoom
    assert p.js("document.scrollingElement.scrollTop") == before       # the page behind didn't move
