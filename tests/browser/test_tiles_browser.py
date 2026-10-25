"""Interactive dashboard tiles and their detail views, in a real browser."""

import json

import pytest

from fixtures import commute_payload, unavailable_weather, weather_payload

LOCATION = {"location": {
    "city": "Bengaluru", "region": "Karnataka", "country": "India", "country_code": "IN",
    "latitude": 12.97, "longitude": 77.59, "timezone": "Asia/Kolkata", "source": "manual",
    "confirmed": True, "provider": "Open-Meteo geocoding", "updated_at": "2026-10-18T00:00:00Z",
}}

IS_OPEN = "document.getElementById('detail').open"
VIEW = "document.getElementById('detail').dataset.view"


@pytest.fixture
def srv(make_server):
    s = make_server()
    (s.data_dir / "location.json").write_text(json.dumps(LOCATION))
    s.restart()
    s.api("POST", "/api/groceries", {"name": "Milk", "quantity": "2 L"})
    s.api("POST", "/api/groceries", {"name": "Eggs"})
    return s


def dashboard(chrome, srv, w=1920, h=1080, touch=False, weather=None, commute=None, path="/", at=None):
    """`at`: a datetime for the page's clock (to match a weather fixture's hour)."""
    p = chrome.new_page()
    p.viewport(w, h, mobile=touch)
    if at is not None:
        p.clock(at.timestamp() * 1000)
    p.fixture("/api/weather", weather or weather_payload(63, 12))
    p.fixture("/api/commute", commute or commute_payload("active"))
    p.goto(f"{srv.url}{path}")
    p.wait_for("document.getElementById('weather').dataset.state !== 'loading'"
               " && document.getElementById('commute').dataset.state !== 'loading'"
               " && document.getElementById('grocery-list').dataset.state === 'ok'")
    return p


def click(p, selector):
    p.js(f"document.querySelector({json.dumps(selector)}).click()")


def key(p, k, code=None, vk=None):
    text = {"Enter": "\r", " ": " "}.get(k)
    p.call("Input.dispatchKeyEvent", type="keyDown", key=k, code=code or k, windowsVirtualKeyCode=vk or 0,
           nativeVirtualKeyCode=vk or 0, **({"text": text, "unmodifiedText": text} if text else {}))
    p.call("Input.dispatchKeyEvent", type="keyUp", key=k, code=code or k, windowsVirtualKeyCode=vk or 0,
           nativeVirtualKeyCode=vk or 0)


SETTLED = "document.getAnimations().every(a => a.playState !== 'running' || a.effect?.getTiming().iterations === Infinity)"


TILES = [
    ("#clock-hm", "clock", "Time & date", None),
    ("#date", "clock", "Time & date", None),
    ("#weather-desc", "weather", "Weather", None),
    (".weather > .tile-arrow", "weather", "Weather", None),
    ("#hourly li", "weather", "Weather", "hourly"),
    ("#daily li", "weather", "Weather", "daily"),
    ("#weather-stats dd", "weather", "Weather", "metrics"),
    (".advice__title", "advice", "Before you go", None),
    (".advice .tile-arrow", "advice", "Before you go", None),
    (".commute__time", "commute", "Commute", None),
    ("#commute .slots", "commute", "Commute", None),
    (".commute .tile-arrow", "commute", "Commute", None),
]


@pytest.mark.parametrize("selector, view, title, section", TILES, ids=[t[0] for t in TILES])
def test_tile_opens_detail_and_back_closes(chrome, srv, selector, view, title, section):
    p = dashboard(chrome, srv)
    before = p.js("history.length")
    click(p, selector)
    p.wait_for(IS_OPEN)
    assert p.js(VIEW) == view
    assert p.text("#detail-title") == title
    assert p.js("location.hash") == f"#/{view}" + (f"/{section}" if section else "")
    assert p.js("history.length") == before + 1          # exactly one entry: no double firing
    assert p.js("document.activeElement.id") == "detail-back"
    if section:
        # The requested section is scrolled into view inside the panel.
        assert p.js(f"""(() => {{ const b = document.getElementById('detail-body').getBoundingClientRect(),
            s = document.getElementById('dsec-{section}').getBoundingClientRect();
            return s.top >= b.top - 2 && s.top < b.top + b.height / 2; }})()""")
    click(p, "#detail-back")
    p.wait_for(f"!{IS_OPEN}")
    assert p.js("location.hash") == ""
    assert p.errors == [], p.errors


def test_escape_backdrop_and_browser_history(chrome, srv):
    p = dashboard(chrome, srv)
    click(p, ".commute__time")
    p.wait_for(IS_OPEN)
    key(p, "Escape", vk=27)
    p.wait_for(f"!{IS_OPEN}")
    click(p, "#weather-desc")
    p.wait_for(IS_OPEN)
    p.js("history.back()")                      # browser Back button
    p.wait_for(f"!{IS_OPEN} && location.hash === ''")
    p.js("history.forward()")                   # and Forward reopens the same view
    p.wait_for(f"{IS_OPEN} && {VIEW} === 'weather'")
    # Clicking the dimmed backdrop (outside the panel) closes it.
    p.call("Input.dispatchMouseEvent", type="mousePressed", x=5, y=5, button="left", clickCount=1)
    p.call("Input.dispatchMouseEvent", type="mouseReleased", x=5, y=5, button="left", clickCount=1)
    p.wait_for(f"!{IS_OPEN}")
    # Back from the panel never leaves the dashboard.
    assert p.js("location.pathname") == "/"


def test_keyboard_open_and_focus_return(chrome, srv):
    p = dashboard(chrome, srv)
    p.js("document.querySelector('.tile-hit').focus()")
    key(p, "Enter", vk=13)
    p.wait_for(f"{IS_OPEN} && {VIEW} === 'clock'")
    assert p.js("document.activeElement.id") == "detail-back"
    key(p, "Escape", vk=27)
    p.wait_for(f"!{IS_OPEN}")
    assert p.js("document.activeElement.classList.contains('tile-hit')")   # focus restored
    p.js("document.querySelector('.commute .tile-arrow').focus()")
    key(p, " ", code="Space", vk=32)
    p.wait_for(f"{IS_OPEN} && {VIEW} === 'commute'")
    # Visible focus ring for keyboard users.
    assert p.js("getComputedStyle(document.getElementById('detail-back'), ':focus-visible') !== null")
    assert p.js("document.querySelector('.tile-hit').getAttribute('aria-label')") == "Time and date details"
    assert p.js("document.querySelector('.commute .tile-arrow').getAttribute('aria-haspopup')") == "dialog"


def test_touch_tap_opens_tile(chrome, srv):
    p = dashboard(chrome, srv, 390, 844, touch=True)
    # Let the last startup requests (Panchang, prices) settle so the layout holds still under the finger.
    pos = "(() => { const r = document.querySelector('.commute__time').getBoundingClientRect(); return [Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)]; })()"
    p.wait_for("window.performance.getEntriesByType('resource').some(r => r.name.includes('/api/prices'))", timeout=10)
    p.js("document.getElementById('commute').scrollIntoView({block: 'center'})")
    p.js("new Promise(r => setTimeout(r, 500))")
    x, y = p.js(pos)
    assert p.js(f"document.elementFromPoint({x}, {y}).closest('[data-tile]').dataset.tile") == "commute"
    # Real touch events (headless Chromium's synthesizeTapGesture never emits the click).
    p.call("Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": x, "y": y}])
    p.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
    p.wait_for(f"{IS_OPEN} && {VIEW} === 'commute'")
    p.wait_for(SETTLED)
    back = p.js("(() => { const r = document.getElementById('detail-back').getBoundingClientRect(); return [r.width, r.height]; })()")
    assert back[1] >= 44 and back[0] >= 44


def test_inner_controls_do_not_open_the_tile(chrome, srv):
    p = dashboard(chrome, srv)
    start = p.js("history.length")
    click(p, "#location-btn")                                # its own dialog
    p.wait_for("document.getElementById('location-dialog').open")
    assert not p.js(IS_OPEN)
    p.js("document.getElementById('location-dialog').close()")
    # One-tap purchase on the dashboard grocery tile: marks the item, opens nothing.
    p.js("[...document.querySelectorAll('#grocery-list .grocery__item button')].find(b => b.textContent.includes('Milk')).click()")
    p.wait_for("!document.getElementById('grocery-list').textContent.includes('Milk')")
    assert not p.js(IS_OPEN)
    assert p.js("location.pathname") == "/" and p.js("history.length") == start
    status, body = srv.api("GET", "/api/groceries")
    assert next(i for i in body["items"] if i["name"] == "Milk")["purchased"] is True


def test_grocery_tile_opens_full_list_with_back(chrome, srv):
    p = dashboard(chrome, srv)
    click(p, "#grocery-count")                              # card body (not a control)
    p.wait_for("location.pathname === '/groceries' && document.querySelectorAll('.g-item').length >= 2")
    assert "from=dashboard" in p.js("location.search")
    # Full management is available: add an item right here.
    p.js("document.getElementById('g-name').value = 'Bread'; document.getElementById('g-composer').requestSubmit();")
    p.wait_for("document.getElementById('g-groups').textContent.includes('Bread')")
    assert p.text("#g-home") == "Dashboard" and p.js("document.getElementById('g-home').getAttribute('aria-label')") == "Back to dashboard"
    click(p, "#g-home")
    p.wait_for("location.pathname === '/' && document.getElementById('grocery-list').textContent.includes('Bread')")
    # The "List" control in the tile is a real link to the same page.
    assert p.js("document.querySelector('.grocery .tile-arrow').getAttribute('href')") == "/groceries?from=dashboard"


def test_deep_link_opens_view_and_back_stays_on_dashboard(chrome, srv):
    p = dashboard(chrome, srv, path="/#/commute")
    p.wait_for(f"{IS_OPEN} && {VIEW} === 'commute'")
    click(p, "#detail-back")
    p.wait_for(f"!{IS_OPEN}")
    assert p.js("location.pathname") == "/" and p.js("location.hash") == ""


# --- what the detail views show ---------------------------------------------------------

def test_weather_detail_with_live_data(chrome, srv, artifacts):
    p = dashboard(chrome, srv, weather=weather_payload(63, 12))
    click(p, "#weather-desc")
    p.wait_for(IS_OPEN)
    body = p.text("#detail-body")
    for expected in ("Feels like", "Humidity", "Wind", "gusts", "UV index", "Rain chance", "Precipitation",
                     "Air quality", "US AQI", "Sunrise", "Sunset", "Next 24 hours", "-day forecast",
                     "Before you go", "Updated"):
        assert expected in body, expected
    assert p.js("document.querySelectorAll('#dsec-hourly tbody tr').length") >= 6
    assert p.js("document.querySelectorAll('.ddays .dday').length") >= 5
    assert p.js("document.querySelector('.dhero .wx-scene').dataset.scene") == "rain"
    assert p.js("document.querySelector('.dhero .wx-scene').dataset.motion") == "live"
    # Every applicable tip is listed, with its explanation.
    n_all = len(weather_payload(63, 12)["recommendations_all"])
    assert p.js("document.querySelectorAll('#dsec-advice .dadvice__item').length") == n_all
    p.screenshot(str(artifacts / "detail-weather-1920.png"))


def test_weather_detail_stale_and_sample_are_labelled(chrome, srv):
    p = dashboard(chrome, srv, weather=weather_payload(95, 16, status="stale"))
    click(p, "#weather-desc")
    p.wait_for(IS_OPEN)
    assert "Weather service unavailable · showing data from" in p.text("#detail-body")
    assert p.js("document.querySelector('.dhero .wx-scene').dataset.motion") == "static"
    p2 = dashboard(chrome, srv, weather=weather_payload(0, 12, is_mock=True))
    click(p2, "#weather-desc")
    p2.wait_for(IS_OPEN)
    assert "Sample data · not live weather" in p2.text("#detail-body")


def test_unavailable_data_states(chrome, srv):
    p = dashboard(chrome, srv, weather=unavailable_weather(), commute=commute_payload("unconfigured"))
    p.js("window.AuraDetail.open('weather')")
    p.wait_for(IS_OPEN)
    assert "Set your home location" in p.text("#detail-body")
    p.js("window.AuraDetail.open('advice')")
    p.wait_for(f"{VIEW} === 'advice'")
    assert "No advice yet" in p.text("#detail-body")
    p.js("window.AuraDetail.open('commute')")
    p.wait_for(f"{VIEW} === 'commute'")
    text = p.text("#detail-body")
    assert "Traffic isn't connected yet" in text and "TomTom API key" in text
    assert " min" not in text                                # no invented durations
    p.js("window.AuraDetail.open('calendar')")
    p.wait_for(f"{VIEW} === 'calendar'")
    assert "No calendar connected" in p.text("#detail-body")


def test_commute_detail_provenance(chrome, srv, artifacts):
    p = dashboard(chrome, srv, commute=commute_payload("active"))
    click(p, ".commute__time")
    p.wait_for(IS_OPEN)
    text = p.text("#detail-body")
    assert "Home → Office" in p.text("#detail-sub")
    assert "Best time to leave" in text and "9:15 AM" in text
    assert "24 min" in text and "with no traffic" in text and "9.8 km" in text
    assert "Forecast · TomTom historical traffic patterns" in text
    assert "Leaving now (live traffic): 41 min" in text
    assert p.js("document.querySelectorAll('.dtable--slots tbody tr').length") == 17
    assert p.js("document.querySelectorAll('.dtable--slots tr.is-best').length") == 1
    assert p.js("document.querySelectorAll('.dtable--slots tr.is-past').length") == 4
    for term in ("Live traffic", "Forecast", "Your history", "No data", "Requests today", "Mon, Tue, Wed, Thu, Fri"):
        assert term in text, term
    for leak in ("13.0087", "77.7381", "12.9927", "77.6995"):
        assert leak not in p.js("document.documentElement.outerHTML")
    p.screenshot(str(artifacts / "detail-commute-1920.png"))


def test_clock_detail_ticks_and_shows_timezone(chrome, srv):
    p = dashboard(chrome, srv)
    click(p, "#clock-hm")
    p.wait_for(IS_OPEN)
    first = p.text("#dclock-time")
    p.wait_for(f"document.getElementById('dclock-time').textContent !== {json.dumps(first)}", timeout=3)
    text = p.text("#detail-body")
    assert "Asia/Kolkata" in text and "UTC+05:30" in text and "Sunrise" in text


def test_reduced_motion_detail(chrome, srv):
    p = dashboard(chrome, srv)
    p.reduced_motion(True)
    click(p, "#weather-desc")
    p.wait_for(IS_OPEN)
    running = p.js("document.getAnimations().filter(a => a instanceof CSSAnimation && a.playState === 'running').length")
    assert running == 0
    assert p.js("document.querySelector('.dhero .wx-scene').dataset.motion") == "reduced"
    p.reduced_motion(False)


# --- responsive detail views -----------------------------------------------------------------

VIEWPORTS = [(320, 568), (375, 812), (390, 844), (430, 932), (768, 1024), (1024, 768),
             (1280, 800), (1440, 900), (1920, 1080)]


@pytest.mark.parametrize("w, h", VIEWPORTS, ids=[f"{w}x{h}" for w, h in VIEWPORTS])
def test_detail_views_fit_every_viewport(chrome, srv, artifacts, w, h):
    touch = w <= 1024
    p = dashboard(chrome, srv, w, h, touch=touch)
    for view in ("clock", "weather", "advice", "commute"):
        p.js(f"window.AuraDetail.open('{view}')")
        p.wait_for(f"{IS_OPEN} && {VIEW} === '{view}'")
        p.wait_for(SETTLED)                     # let the 0.24 s open animation finish
        r = p.js("""(() => {
            const d = document.getElementById('detail').getBoundingClientRect();
            const b = document.getElementById('detail-body');
            const back = document.getElementById('detail-back').getBoundingClientRect();
            const problems = [];
            if (d.left < -0.5 || d.right > innerWidth + 0.5 || d.top < -0.5 || d.bottom > innerHeight + 0.5) problems.push('panel off-screen');
            if (b.scrollWidth > b.clientWidth + 1) problems.push(`body overflows sideways by ${b.scrollWidth - b.clientWidth}px`);
            if (document.documentElement.scrollWidth > innerWidth) problems.push('page scrolls sideways');
            if (back.height < 44 || back.top < 0) problems.push('back button too small or hidden');
            for (const e of b.querySelectorAll('.dmetric, .dadvice__item, .drec, .dempty, .dhero, .dday, .dtable-wrap')) {
              const er = e.getBoundingClientRect();
              if (er.right > b.getBoundingClientRect().right + 1) problems.push(`clipped ${e.className}`);
            }
            return problems; })()""")
        assert r == [], (view, r)
        if view in ("weather", "commute"):
            p.screenshot(str(artifacts / f"detail-{view}-{w}x{h}.png"))
        p.js("window.AuraDetail.close()")
        p.wait_for(f"!{IS_OPEN}")
    assert p.errors == [], p.errors
