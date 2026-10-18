"""Settings Center and arrow-only tile CTAs, in a real browser."""

import json

import pytest

from fixtures import commute_payload, weather_payload

ENV = {"AURA_COMMUTE_HOME": "13.0087307,77.7381464", "AURA_COMMUTE_OFFICE": "12.9927608,77.6995722"}


@pytest.fixture
def srv(make_server):
    return make_server(**ENV)


def dashboard(chrome, srv, w=1920, h=1080, touch=False):
    p = chrome.new_page()
    p.viewport(w, h, mobile=touch)
    p.fixture("/api/weather", weather_payload(63, 12))
    p.fixture("/api/commute", commute_payload("active"))
    p.goto(f"{srv.url}/")
    p.wait_for("document.getElementById('commute').dataset.state === 'active'")
    return p


def settings_page(chrome, srv, w=1440, h=900, touch=False, path="/settings"):
    p = chrome.new_page()
    p.viewport(w, h, mobile=touch)
    p.goto(f"{srv.url}{path}")
    p.wait_for("!document.getElementById('s-main').hidden", timeout=15)
    return p


def field(p, form, name):
    return f"document.querySelector('#{form} [data-field=\"{name}\"]')"


# --- gear icon ------------------------------------------------------------------------

def test_gear_opens_settings_and_back_returns(chrome, srv):
    p = dashboard(chrome, srv)
    g = p.js("""(() => { const a = document.getElementById('settings-link'), r = a.getBoundingClientRect();
        return {label: a.getAttribute('aria-label'), w: r.width, h: r.height, right: innerWidth - r.right, top: r.top,
                text: a.textContent.trim()}; })()""")
    assert g["label"] == "Settings" and g["text"] == ""
    assert g["w"] >= 44 and g["h"] >= 44
    assert g["right"] < 80 and g["top"] < 80                      # top-right corner
    p.js("document.getElementById('settings-link').focus()")
    for t in ("keyDown", "keyUp"):
        p.call("Input.dispatchKeyEvent", type=t, key="Enter", code="Enter", windowsVirtualKeyCode=13,
               **({"text": "\r"} if t == "keyDown" else {}))
    p.wait_for("location.pathname === '/settings' && !document.getElementById('s-main').hidden", timeout=15)
    p.click("#s-back")
    p.wait_for("location.pathname === '/' && !!document.getElementById('settings-link')")
    assert p.errors == []


@pytest.mark.parametrize("w, h, touch", [(320, 568, True), (768, 1024, True), (1920, 1080, False)])
def test_gear_never_overlaps_header_content(chrome, srv, w, h, touch):
    p = dashboard(chrome, srv, w, h, touch)
    overlaps = p.js("""(() => { const g = document.getElementById('settings-link').getBoundingClientRect();
        const out = [];
        for (const sel of ['.brand', '#clock', '#date', '.place', '#weather', '#preview-pill']) {
          const e = document.querySelector(sel); if (!e || e.hidden) continue;
          const r = e.getBoundingClientRect(); if (!r.width) continue;
          if (Math.min(g.right, r.right) - Math.max(g.left, r.left) > 0 && Math.min(g.bottom, r.bottom) - Math.max(g.top, r.top) > 0) out.push(sel);
        } return out; })()""")
    assert overlaps == []
    assert p.js("document.getElementById('settings-link').getBoundingClientRect().right <= innerWidth")


# --- arrow-only tile CTAs ----------------------------------------------------------------

def test_tiles_show_only_a_bare_arrow(chrome, srv):
    p = dashboard(chrome, srv)
    info = p.js("""(() => [...document.querySelectorAll('.card[data-open]')].map(card => {
        const arrows = card.querySelectorAll(':scope > .tile-arrow');
        const a = arrows[0]; const cs = a && getComputedStyle(a); const r = a?.getBoundingClientRect(), c = card.getBoundingClientRect();
        return {card: card.className, n: arrows.length, text: a?.textContent.trim(), bg: cs?.backgroundColor,
                border: cs?.borderTopWidth, svg: !!a?.querySelector('svg'),
                bottomRight: r && (c.right - r.right) < 30 && (c.bottom - r.bottom) < 30,
                label: a?.getAttribute('aria-label')}; }))()""")
    assert len(info) == 4
    for t in info:
        assert t["n"] == 1 and t["svg"] and t["text"] == "", t
        assert t["bg"] in ("rgba(0, 0, 0, 0)", "transparent") and t["border"] == "0px", t
        assert t["bottomRight"] and t["label"], t
    body = p.js("document.getElementById('shell').innerText")
    for word in ("Details", "View details", "See more", "Explore"):
        assert word not in body
    # The clock tile has a decorative arrow inside its own button, hidden from screen readers.
    assert p.js("document.querySelector('.tile-hit .tile-arrow').getAttribute('aria-hidden')") == "true"
    # Nothing non-clickable gets an arrow.
    assert p.js("document.querySelectorAll('.topbar .tile-arrow, .daily .tile-arrow, .place .tile-arrow').length") == 0


def test_arrow_opens_exactly_once(chrome, srv):
    p = dashboard(chrome, srv)
    # Count history entries pushed and views opened per click (catches double handlers).
    p.js("""window.__pushes = 0; const orig = history.pushState.bind(history);
            history.pushState = (...a) => { window.__pushes++; return orig(...a); };""")
    for card, view in (("weather", "weather"), ("commute", "commute"), ("advice", "advice")):
        p.js("window.__pushes = 0")
        p.click(f".{card} > .tile-arrow")
        p.wait_for(f"document.getElementById('detail').open && document.getElementById('detail').dataset.view === '{view}'")
        assert p.js("window.__pushes") == 1
        p.click("#detail-back")
        p.wait_for("!document.getElementById('detail').open")
    p.click(".grocery > .tile-arrow")
    p.wait_for("location.pathname === '/groceries'")


@pytest.mark.parametrize("w, h", [(1920, 1080), (1600, 900), (1366, 768), (1280, 720), (3840, 2160),
                                  (1512, 860), (1024, 768), (390, 844)])
def test_arrow_does_not_overlap_tile_text(chrome, srv, w, h):
    for name in ("Basmati rice", "Tomatoes", "Coffee", "Dish soap", "Paneer", "Milk"):
        srv.api("POST", "/api/groceries", {"name": name})
    p = dashboard(chrome, srv, w, h, touch=w < 800)
    hits = p.js("""(() => { const out = [];
        for (const a of document.querySelectorAll('.card > .tile-arrow')) {
          const r = a.getBoundingClientRect(), card = a.parentElement;
          const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
          let n; while ((n = walker.nextNode())) {
            if (!n.textContent.trim() || a.contains(n)) continue;
            const range = document.createRange(); range.selectNodeContents(n);
            for (const t of range.getClientRects()) {
              if (Math.min(r.right, t.right) - Math.max(r.left, t.left) > 1 && Math.min(r.bottom, t.bottom) - Math.max(r.top, t.top) > 1)
                out.push(card.className + ': ' + n.textContent.trim().slice(0, 30));
            }
          }
        } return out; })()""")
    assert hits == []


# --- settings page through the UI ----------------------------------------------------------

def test_save_cancel_and_live_effect_on_dashboard(chrome, srv):
    dash = dashboard(chrome, srv)
    assert ":" in dash.text("#clock-hm") and dash.text("#clock-ampm") in ("AM", "PM")
    p = settings_page(chrome, srv)
    form = "display"
    assert p.js("document.querySelector('#display .s-save').disabled")          # nothing to save yet
    p.js("document.querySelector('#display input[value=\"24h\"]').click()")
    p.wait_for("!document.querySelector('#display .s-save').disabled && document.getElementById('display').classList.contains('is-dirty')")
    p.click("#display .s-cancel")                                                 # Cancel reverts
    assert p.js("document.querySelector('#display input[value=\"12h\"]').checked")
    p.js("document.querySelector('#display input[value=\"24h\"]').click()")
    p.js("document.querySelector('#display input[value=\"fahrenheit\"]').click()")
    p.click("#display .s-save")
    p.wait_for("document.querySelector('#display .s-status').dataset.kind === 'ok'")
    assert "Saved" in p.text("#display .s-status")
    status, body = srv.api("GET", "/api/config")
    assert body["clock_format"] == "24h" and body["display"]["temperature_unit"] == "fahrenheit"
    # The open dashboard switches to 24-hour and °F by itself (live update, no reload).
    dash.wait_for("document.getElementById('clock-ampm').textContent === ''", timeout=10)
    dash.wait_for("document.getElementById('weather-temp').textContent === '84°'", timeout=10)   # 29 °C
    assert p.errors == [] and dash.errors == []


def test_invalid_coordinates_show_field_errors_and_nothing_is_saved(chrome, srv):
    p = settings_page(chrome, srv, path="/settings#locations")
    p.js(f"{field(p, 'locations', 'home.latitude')}.value = '95'; {field(p, 'locations', 'home.latitude')}.dispatchEvent(new Event('input', {{bubbles: true}}))")
    p.click("#locations .s-save")
    p.wait_for("document.querySelector('#locations .s-status').dataset.kind === 'error'")
    assert p.js(f"{field(p, 'locations', 'home.latitude')}.getAttribute('aria-invalid')") == "true"
    err = p.text("#locations .s-err")
    assert "less than or equal to 90" in err
    assert p.js("document.activeElement.dataset.field") == "home.latitude"      # focus moved to the problem
    _, body = srv.api("GET", "/api/settings")
    assert body["values"]["locations"]["home"]["latitude"] == 13.0087307
    # Fix it and save: persisted and applied to the commute.
    p.js(f"{field(p, 'locations', 'home.latitude')}.value = '13.02'; {field(p, 'locations', 'home.latitude')}.dispatchEvent(new Event('input', {{bubbles: true}}))")
    p.click("#locations .s-save")
    p.wait_for("document.querySelector('#locations .s-status').dataset.kind === 'ok'")
    _, body = srv.api("GET", "/api/settings")
    assert body["values"]["locations"]["home"]["latitude"] == 13.02
    expected = {"Reset", "Cancel", "Save"}
    assert expected <= set(p.js("[...document.querySelectorAll('#locations .s-actions button')].map(b => b.textContent.split(' ')[0])"))


def test_commute_settings_change_backend_schedule(chrome, srv):
    p = settings_page(chrome, srv, path="/settings#commute")
    p.js(f"""{field(p, 'commute', 'window_start')}.value = '07:30';
             {field(p, 'commute', 'slot_minutes')}.value = '30';
             {field(p, 'commute', 'preferred_arrival')}.value = '09:45';
             document.querySelector('#commute input[value="fri"]').click();
             document.getElementById('commute').dispatchEvent(new Event('change'));""")
    p.click("#commute .s-save")
    p.wait_for("document.querySelector('#commute .s-status').dataset.kind === 'ok'")
    _, c = srv.api("GET", "/api/commute")
    assert c["slot_minutes"] == 30 and c["days"] == ["mon", "tue", "wed", "thu"]
    assert c["preferred_arrival"] == "09:45"
    assert c["window_start"] is None or "07:30" in c["window_start"]
    # Bad window is rejected on the server and shown next to the field.
    p.js(f"""{field(p, 'commute', 'window_end')}.value = '06:00';
             document.getElementById('commute').dispatchEvent(new Event('change'));""")
    p.click("#commute .s-save")
    p.wait_for("document.querySelector('#commute .s-status').dataset.kind === 'error'")
    assert "after the start time" in p.text("#commute .s-err")


def test_reset_needs_confirmation(chrome, srv):
    srv.api("PATCH", "/api/settings/weather", {"refresh_minutes": 30})
    p = settings_page(chrome, srv, path="/settings#weather")
    assert p.js(f"{field(p, 'weather', 'refresh_minutes')}.value") == "30"
    p.click("#weather .s-reset")
    p.wait_for("document.getElementById('s-confirm').open")
    p.click("#sc-cancel")
    _, body = srv.api("GET", "/api/settings")
    assert body["values"]["weather"]["refresh_minutes"] == 30
    p.click("#weather .s-reset")
    p.wait_for("document.getElementById('s-confirm').open")
    p.click("#sc-ok")
    p.wait_for(f"{field(p, 'weather', 'refresh_minutes')}.value === '15'")


def test_network_and_system_sections_render(chrome, srv):
    p = settings_page(chrome, srv)
    p.wait_for("document.getElementById('net-status').children.length >= 5", timeout=15)
    net = p.text("#net-status")
    assert "Hostname" in net and "Ethernet" in net
    p.wait_for("document.getElementById('sys-info').children.length >= 8", timeout=15)
    sysinfo = p.text("#sys-info")
    for k in ("Backend", "Healthy", "AURA version", "Python", "Storage free", "Memory available"):
        assert k in sysinfo, k
    assert "nmcli" in p.text(".s-details")                                   # manual Wi-Fi instructions
    assert p.errors == []


def test_quick_add_editor(chrome, srv):
    p = settings_page(chrome, srv, path="/settings#groceries")
    p.js("document.getElementById('qa-new').value = 'Jaggery'")
    p.click("#qa-add")
    p.js("document.querySelector('#qa-list li .s-chip__x').click()")          # remove the first one
    p.click("#groceries .s-save")
    p.wait_for("document.querySelector('#groceries .s-status').dataset.kind === 'ok'")
    _, body = srv.api("GET", "/api/settings")
    qa = body["values"]["groceries"]["quick_add"]
    assert qa[-1] == "Jaggery" and "Milk" not in qa


# --- responsive -----------------------------------------------------------------------------

VIEWPORTS = [(320, 568), (375, 812), (390, 844), (430, 932), (768, 1024), (1024, 768),
             (1280, 800), (1440, 900), (1920, 1080)]


@pytest.mark.parametrize("w, h", VIEWPORTS, ids=[f"{w}x{h}" for w, h in VIEWPORTS])
def test_settings_layout(chrome, srv, artifacts, w, h):
    touch = w <= 1024
    p = settings_page(chrome, srv, w, h, touch)
    r = p.js("""(() => { const problems = [];
        if (document.documentElement.scrollWidth > innerWidth) problems.push('page scrolls sideways ' + (document.documentElement.scrollWidth - innerWidth));
        for (const c of document.querySelectorAll('.s-card')) { const b = c.getBoundingClientRect();
          if (b.left < -1 || b.right > innerWidth + 1) problems.push('card off-screen ' + c.id); }
        if (matchMedia('(pointer: coarse)').matches) {
          for (const e of document.querySelectorAll('.s-main button, .s-main input:not([type=checkbox]):not([type=radio]), .s-main select, .s-nav a, .s-back')) {
            const b = e.getBoundingClientRect(); if (!b.width || e.closest('[hidden]')) continue;
            if (b.height < 40) problems.push(`small ${e.id || e.className || e.tagName} ${Math.round(b.height)}`);
          }
        }
        return problems; })()""")
    assert r == [], r
    # Save/Cancel stay reachable for a long section while it has unsaved changes.
    p.js(f"{field(p, 'commute', 'window_start')}.value = '07:15'; document.getElementById('commute').dispatchEvent(new Event('change'))")
    p.js("document.querySelector('#commute [data-field=\"window_start\"]').scrollIntoView({block: 'start'})")
    p.js("new Promise(r => setTimeout(r, 200))")
    visible = p.js("""(() => { const b = document.querySelector('#commute .s-save').getBoundingClientRect();
        return b.top >= 0 && b.bottom <= innerHeight + 1 && b.right <= innerWidth; })()""")
    assert visible
    p.screenshot(str(artifacts / f"settings-{w}x{h}.png"))
    # Confirm dialog fits.
    p.click("#weather .s-reset")
    p.wait_for("document.getElementById('s-confirm').open")
    assert p.js("""(() => { const r = document.getElementById('s-confirm').getBoundingClientRect();
        return r.left >= 0 && r.right <= innerWidth && r.bottom <= innerHeight + 1; })()""")
    p.click("#sc-cancel")
    assert p.errors == [], p.errors


def test_dashboard_reloads_itself_after_server_restart(chrome, srv):
    """The always-on entrance display picks up a new version without anyone touching it."""
    p = dashboard(chrome, srv)
    p.js("window.__marker = 'old page'")
    srv.restart()
    p.wait_for("window.__marker === undefined && !!document.getElementById('settings-link')"
               " && document.getElementById('commute').dataset.state === 'active'", timeout=30)
    p.errors[:] = [e for e in p.errors if "ERR_CONNECTION_REFUSED" not in e and "/api/" not in e]
    assert p.errors == []
