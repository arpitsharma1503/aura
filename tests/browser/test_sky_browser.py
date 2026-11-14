"""Sun & Moon on the weather card and in its detail view, in a real browser."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from fixtures import at_hour, weather_payload
from test_tiles_browser import IS_OPEN, LOCATION, VIEW, dashboard

IST = ZoneInfo("Asia/Kolkata")
WAXING = datetime(2026, 10, 14, 20, tzinfo=IST)     # waxing crescent, ~15 %
WANING = datetime(2026, 11, 5, 6, tzinfo=IST)       # waning crescent, ~18 %
GIBBOUS = datetime(2026, 10, 22, 20, tzinfo=IST)    # waxing gibbous, ~85 %


@pytest.fixture
def srv(make_server):
    s = make_server()
    (s.data_dir / "location.json").write_text(json.dumps(LOCATION))
    s.restart()
    s.api("POST", "/api/groceries", {"name": "Milk", "quantity": "2 L"})   # the dashboard helper waits for a list
    return s


def lit_center(p, root: str) -> float:
    """Horizontal centre of the lit part of the moon drawn inside `root`, in SVG units (disc centre = 50)."""
    return p.js(f"""(() => {{ const s = document.querySelector({json.dumps(root)} + ' svg.moon');
        const lit = s.querySelector('clipPath > *'); const b = lit.getBBox();
        return b.width > 0 && s.getBoundingClientRect().width > 0 ? b.x + b.width / 2 : null; }})()""")


def test_tile_header_shows_moon_phase(chrome, srv):
    p = dashboard(chrome, srv, weather=weather_payload(0, 21, moon_at=GIBBOUS), at=at_hour(21))
    p.wait_for("!!document.querySelector('#weather-moon:not([hidden]) svg.moon')")
    assert p.text(".weather__moon-name") == "Waxing Gibbous"
    assert p.text(".weather__moon-pct").endswith("%")
    assert "days old" in p.js("document.getElementById('weather-moon').title")
    assert p.errors == [], p.errors


@pytest.mark.parametrize("when, latitude, side", [
    (WAXING, 12.97, "right"), (WANING, 12.97, "left"),          # northern hemisphere
    (WAXING, -33.87, "left"), (WANING, -33.87, "right"),        # southern: mirrored
])
def test_moon_orientation(chrome, srv, when, latitude, side):
    # By day the header shows its own small moon (at night the big picture does).
    p = dashboard(chrome, srv, weather=weather_payload(0, 12, latitude=latitude, moon_at=when), at=at_hour(12))
    p.wait_for("!!document.querySelector('#weather-moon svg.moon clipPath > *')")
    assert p.js("document.querySelector('#weather-moon svg.moon').dataset.litSide") == side
    cx = lit_center(p, "#weather-moon")
    assert cx is not None
    assert (cx > 60) if side == "right" else (cx < 40), cx


def test_terminator_geometry(chrome, srv):
    """Terminator = half-ellipse with x-radius r·|2k − 1| (= r·|cos i|); limb = semicircle."""
    p = dashboard(chrome, srv)
    p.wait_for("!!window.AuraSky")
    for k, side in ((0.25, "right"), (0.75, "right"), (0.25, "left"), (0.75, "left")):
        d = p.js(f"window.AuraSky.litPath({k}, '{side}')")
        rx = float(d.split(" A")[2].split()[0])
        assert rx == pytest.approx(46 * abs(2 * k - 1), abs=1e-3), d
    # Sample the lit area along the equator (y = 50): its width is 2r·k, on the requested side.
    lit = p.js("""(() => { const out = {};
        for (const side of ['right', 'left']) for (const k of [0.1, 0.5, 0.9]) {
          const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
          const path = document.createElementNS(ns, 'path'); path.setAttribute('d', window.AuraSky.litPath(k, side));
          svg.append(path); document.body.append(svg);
          const xs = []; for (let x = 4.05; x < 96; x += 0.1) if (path.isPointInFill(new DOMPoint(x, 50))) xs.push(x);
          out[`${side}${k}`] = { width: xs.length / 10, min: Math.min(...xs), max: Math.max(...xs) }; svg.remove(); }
        return out; })()""")
    for k in (0.1, 0.5, 0.9):
        right, left = lit[f"right{k}"], lit[f"left{k}"]
        assert right["width"] == pytest.approx(92 * k, abs=0.3), (k, right)
        assert left["width"] == pytest.approx(92 * k, abs=0.3), (k, left)
        assert right["max"] > 95 and left["min"] < 5          # lit limb on the requested side
    assert p.js("window.AuraSky.litPath(0, 'right')") == ""
    assert p.js("window.AuraSky.litPath(1, 'right')") == "full"


@pytest.mark.parametrize("hour, status, text", [
    (5, "before_sunrise", "Before sunrise"),
    (12, "day", "Daytime"),
    (21, "after_sunset", "Sunrise tomorrow"),
])
def test_detail_sun_and_moon(chrome, srv, artifacts, hour, status, text):
    p = dashboard(chrome, srv, weather=weather_payload(0, hour), at=at_hour(hour))
    p.click("#weather-moon")
    p.wait_for(f"{IS_OPEN} && {VIEW} === 'weather' && !!document.getElementById('dsky-sun')")
    assert p.js("location.hash") == "#/weather/astro"
    sun = p.js("document.querySelector('#dsky-sun .sunarc').dataset.status")
    assert sun == status
    progress = float(p.js("document.querySelector('#dsky-sun .sunarc').dataset.progress"))
    assert progress == {"before_sunrise": 0, "after_sunset": 1}.get(status, progress)
    if status == "day":
        expected = (12 * 60 - (6 * 60 + 10)) / ((17 * 60 + 59) - (6 * 60 + 10))
        assert progress == pytest.approx(expected, abs=0.002)
    body = p.text("#dsec-astro")
    assert text in body
    for label in ("Sunrise", "Sunset", "Daylight", "11 h 49 min", "Illumination", "Age", "days"):
        assert label in body, label
    assert p.js("document.querySelectorAll('#dsky-moon .dsky__next li').length") == 4
    p.screenshot(str(artifacts / f"detail-sky-{status}.png"))
    assert p.errors == [], p.errors


def test_moon_only_when_weather_is_unavailable(chrome, srv):
    payload = weather_payload(0, 12)
    payload.update(status="error", reason="provider_error", message="Weather service unavailable", data=None)
    payload["astronomy"]["sun"] = None
    p = dashboard(chrome, srv, weather=payload)
    p.wait_for("!!document.querySelector('#weather-moon:not([hidden])')")
    p.js("window.AuraDetail.open('weather', 'astro')")
    p.wait_for(f"{IS_OPEN} && !!document.getElementById('dsky-moon')")
    body = p.text("#detail-body")
    assert "Weather couldn't be loaded" in body
    assert "Sunrise and sunset appear when weather data is available." in body
    assert p.js("!!document.querySelector('#dsky-sun .sunarc')") is False


def test_no_astronomy_hides_everything(chrome, srv):
    payload = weather_payload(0, 12)
    payload["astronomy"] = None
    p = dashboard(chrome, srv, weather=payload)
    assert p.js("document.getElementById('weather-moon').hidden") is True
    p.js("window.AuraDetail.open('weather')")
    p.wait_for(IS_OPEN)
    assert "Sun & Moon" not in p.text("#detail-body")


ENTRANCE = [(1920, 1080), (1600, 900), (1366, 768), (1280, 720)]


@pytest.mark.parametrize("w, h", ENTRANCE, ids=[f"{w}x{h}" for w, h in ENTRANCE])
def test_entrance_screens_fit(chrome, srv, artifacts, w, h):
    p = dashboard(chrome, srv, w, h, weather=weather_payload(0, 21, moon_at=GIBBOUS), at=at_hour(21))
    p.wait_for("!!document.querySelector('#weather-moon:not([hidden]) svg.moon')")
    r = p.js("""(() => { const t = document.getElementById('weather').getBoundingClientRect();
        const m = document.getElementById('weather-moon').getBoundingClientRect();
        const a = document.querySelector('#weather > .tile-arrow').getBoundingClientRect();
        const n = document.querySelector('.weather__moon-name');
        return { inside: m.left >= t.left && m.right <= t.right && m.top >= t.top && m.bottom <= t.bottom,
                 clearOfArrow: m.right <= a.left + 1, nameFits: n.scrollWidth <= n.clientWidth + 1,
                 tileClipped: document.getElementById('weather').scrollHeight - document.getElementById('weather').clientHeight,
                 hscroll: document.documentElement.scrollWidth - innerWidth,
                 vscroll: document.documentElement.scrollHeight - innerHeight }; })()""")
    assert r == {"inside": True, "clearOfArrow": True, "nameFits": True, "tileClipped": r["tileClipped"],
                 "hscroll": r["hscroll"], "vscroll": r["vscroll"]}, r
    assert r["tileClipped"] <= 2 and r["hscroll"] <= 0 and r["vscroll"] <= 0, r
    p.screenshot(str(artifacts / f"dashboard-moon-{w}x{h}.png"))
    p.js("window.AuraDetail.open('weather', 'astro')")
    p.wait_for(f"{IS_OPEN} && !!document.getElementById('dsky-moon')")
    p.js("new Promise(r => setTimeout(r, 400))")
    over = p.js("""(() => { const b = document.getElementById('detail-body'); const br = b.getBoundingClientRect();
        const bad = [...b.querySelectorAll('.dsky__card, .dsky__card *')].filter(e => e.getBoundingClientRect().right > br.right + 1);
        return { sideways: b.scrollWidth - b.clientWidth, clipped: bad.map(e => e.className.baseVal ?? e.className) }; })()""")
    assert over["sideways"] <= 1 and over["clipped"] == [], over
    p.screenshot(str(artifacts / f"detail-sky-{w}x{h}.png"))
    assert p.errors == [], p.errors


# --- the big picture beside the temperature -------------------------------------------------

ART = "document.querySelector('#weather-art')"


def art(p):
    return p.js(f"""(() => {{ const a = {ART}; return {{
        kind: document.getElementById('weather').dataset.art,
        moon: !!a.querySelector('.moon'), sun: !!a.querySelector('.sky-sun'),
        cloud: !!a.querySelector('.wx-cloud'), rain: !!a.querySelector('.wx-drop'), bolt: !!a.querySelector('.wx-bolt'),
        side: a.querySelector('.moon')?.dataset.litSide || null }}; }})()""")


def test_night_clear_shows_the_phase_moon(chrome, srv):
    p = dashboard(chrome, srv, weather=weather_payload(0, 21, moon_at=GIBBOUS), at=at_hour(21))
    p.wait_for(f"!!{ART}.querySelector('.moon')")
    assert art(p) == {"kind": "moon", "moon": True, "sun": False, "cloud": False, "rain": False,
                      "bolt": False, "side": "right"}
    # A gibbous Moon, not the old crescent: the lit part covers most of the disc.
    lit = p.js(f"""(() => {{ const g = {ART}.querySelector('.moon'), path = g.querySelector('clipPath > *');
        const xs = []; for (let x = 4.05; x < 96; x += 0.1) if (path.isPointInFill(new DOMPoint(x, 50))) xs.push(x);
        return xs.length / 920; }})()""")
    assert lit == pytest.approx(0.85, abs=0.06)
    # The header keeps the phase name and percentage; its small disc would only repeat the picture.
    assert p.js("getComputedStyle(document.querySelector('#weather-moon svg')).display") == "none"
    assert p.text(".weather__moon-name") == "Waxing Gibbous" and p.text(".weather__moon-pct") == "85%"


def test_southern_night_moon_is_mirrored(chrome, srv):
    p = dashboard(chrome, srv, weather=weather_payload(0, 21, latitude=-33.87, moon_at=WAXING), at=at_hour(21))
    p.wait_for(f"!!{ART}.querySelector('.moon')")
    assert art(p)["side"] == "left"


def test_day_clear_shows_the_glowing_sun(chrome, srv):
    p = dashboard(chrome, srv, weather=weather_payload(0, 12), at=at_hour(12))
    p.wait_for(f"!!{ART}.querySelector('.sky-sun')")
    assert art(p) == {"kind": "sun", "moon": False, "sun": True, "cloud": False, "rain": False,
                      "bolt": False, "side": None}
    assert p.js("getComputedStyle(document.querySelector('#weather-moon svg')).display") != "none"


@pytest.mark.parametrize("code, hour, body", [(2, 21, "moon"), (3, 21, "moon"), (2, 12, "sun"), (3, 12, "sun")])
def test_clouds_partly_cover_the_sun_or_moon(chrome, srv, code, hour, body):
    p = dashboard(chrome, srv, weather=weather_payload(code, hour, moon_at=GIBBOUS), at=at_hour(hour))
    p.wait_for(f"!!{ART}.querySelector('.wx-cloud')")
    a = art(p)
    assert a["kind"] == body and a[body] and a["cloud"] and not a["rain"]


@pytest.mark.parametrize("code, hour, mark", [(63, 21, "rain"), (95, 12, "bolt"), (53, 21, "rain")])
def test_wet_weather_keeps_its_condition_art(chrome, srv, code, hour, mark):
    p = dashboard(chrome, srv, weather=weather_payload(code, hour, moon_at=GIBBOUS), at=at_hour(hour))
    p.wait_for(f"!!{ART}.querySelector('svg')")
    a = art(p)
    assert a["kind"] == "icon" and a[mark] and not a["moon"] and not a["sun"]
    assert p.js("getComputedStyle(document.querySelector('#weather-moon svg')).display") != "none"


def test_without_astronomy_the_plain_icon_stays(chrome, srv):
    payload = weather_payload(0, 21)
    payload["astronomy"] = None
    p = dashboard(chrome, srv, weather=payload)
    p.wait_for(f"!!{ART}.querySelector('svg')")
    a = art(p)
    assert a["kind"] == "icon" and not a["moon"] and not a["sun"]


RECTS = """(() => Object.fromEntries(['weather-art', 'weather-temp', 'weather-desc', 'weather-range', 'daily']
    .map(id => { const r = document.getElementById(id).getBoundingClientRect();
                 return [id, [r.left, r.top, r.width, r.height].map(v => Math.round(v * 10) / 10)]; })))()"""


@pytest.mark.parametrize("w, h", ENTRANCE, ids=[f"{w}x{h}" for w, h in ENTRANCE])
@pytest.mark.parametrize("hour", [12, 21], ids=["day", "night"])
def test_picture_keeps_the_tile_layout(chrome, srv, artifacts, w, h, hour):
    plain = weather_payload(0, hour, moon_at=GIBBOUS)
    plain["astronomy"] = None
    p0 = dashboard(chrome, srv, w, h, weather=plain, at=at_hour(hour))
    p0.wait_for(f"!!{ART}.querySelector('svg')")
    before = p0.js(RECTS)
    p = dashboard(chrome, srv, w, h, weather=weather_payload(0, hour, moon_at=GIBBOUS), at=at_hour(hour))
    p.wait_for(f"!!{ART}.querySelector('.moon, .sky-sun')")
    after = p.js(RECTS)
    assert after == before
    clip = p.js(f"""(() => {{ const t = document.getElementById('weather'); return t.scrollHeight - t.clientHeight; }})()""")
    assert clip <= 2
    p.screenshot(str(artifacts / f"dashboard-sky-{'day' if hour == 12 else 'night'}-{w}x{h}.png"))
    assert p.errors == [], p.errors



# --- the detail view uses the same picture and the same clock --------------------------------

HERO = """(() => { const d = document.querySelector('.dhero'), a = d.querySelector('.dhero__art');
    return { kind: d.dataset.art, moon: !!a.querySelector('.moon'), sun: !!a.querySelector('.sky-sun'),
             side: a.querySelector('.moon')?.dataset.litSide || null,
             scene: d.querySelector('.wx-scene').dataset.scene,
             tileScene: document.getElementById('wx-scene').dataset.scene,
             sun_status: document.getElementById('dsky-sun').dataset.status }; })()"""


@pytest.mark.parametrize("w, h", ENTRANCE, ids=[f"{w}x{h}" for w, h in ENTRANCE])
@pytest.mark.parametrize("hour", [12, 21], ids=["day", "night"])
def test_detail_hero_matches_the_tile(chrome, srv, artifacts, w, h, hour):
    p = dashboard(chrome, srv, w, h, weather=weather_payload(0, hour, moon_at=GIBBOUS), at=at_hour(hour))
    p.wait_for(f"!!{ART}.querySelector('.moon, .sky-sun')")
    tile = art(p)
    p.js("window.AuraDetail.open('weather')")
    p.wait_for(f"{IS_OPEN} && !!document.querySelector('.dhero__art svg')")
    hero = p.js(HERO)
    night = hour == 21
    assert hero["kind"] == tile["kind"] == ("moon" if night else "sun")
    assert hero["moon"] == night and hero["sun"] == (not night) and hero["side"] == tile["side"]
    assert hero["scene"] == hero["tileScene"] == ("clear-night" if night else "clear-day")
    assert hero["sun_status"] == ("after_sunset" if night else "day")
    # The hero picture fills the hero's own art box, larger than the tile's.
    sizes = p.js("""(() => [document.querySelector('.dhero__art svg'), document.querySelector('#weather-art svg')]
        .map(e => e.getBoundingClientRect().width))()""")
    if night:
        assert sizes[0] > sizes[1] > 0
    else:                                   # on a clear day the tile's sun is the scene's rays: no second, small sun
        assert sizes[0] > 0 and sizes[1] == 0
    p.js("new Promise(r => setTimeout(r, 400))")
    sideways = p.js("(() => { const b = document.getElementById('detail-body'); return b.scrollWidth - b.clientWidth; })()")
    assert sideways <= 1
    p.screenshot(str(artifacts / f"detail-weather-{'night' if night else 'day'}-{w}x{h}.png"))
    assert p.errors == [], p.errors


@pytest.mark.parametrize("code", [63, 95, 45])
def test_detail_hero_keeps_wet_weather_art(chrome, srv, code):
    p = dashboard(chrome, srv, weather=weather_payload(code, 21, moon_at=GIBBOUS), at=at_hour(21))
    p.js("window.AuraDetail.open('weather')")
    p.wait_for(f"{IS_OPEN} && !!document.querySelector('.dhero__art svg')")
    hero = p.js(HERO)
    assert hero["kind"] == "icon" and not hero["moon"] and not hero["sun"]


def test_page_clock_drives_tile_and_sun_card_alike(chrome, srv):
    """Regression: the fixture's astronomy is computed for noon, but the page clock says 22:00.
    Everything on the page follows the page clock, so nothing contradicts the greeting."""
    p = dashboard(chrome, srv, weather=weather_payload(0, 12, moon_at=GIBBOUS), at=at_hour(22))
    p.wait_for(f"!!{ART}.querySelector('.moon, .sky-sun')")
    assert art(p)["kind"] == "moon"
    assert p.text("#greeting") == "Good night"
    p.js("window.AuraDetail.open('weather', 'astro')")
    p.wait_for(f"{IS_OPEN} && !!document.getElementById('dsky-sun')")
    assert p.js("document.getElementById('dsky-sun').dataset.status") == "after_sunset"
    assert p.js("document.querySelector('.dhero').dataset.art") == "moon"
    assert "After sunset" in p.text("#dsky-sun")


@pytest.mark.parametrize("w, h", [(1920, 1080), (1280, 720)])
def test_weather_detail_does_not_repeat_itself(chrome, srv, artifacts, w, h):
    """Sunrise/sunset only on the Sun card, "Feels like" only in the hero line."""
    p = dashboard(chrome, srv, w=w, h=h, weather=weather_payload(0, 12), at=at_hour(12))
    p.js("window.AuraDetail.open('weather')")
    p.wait_for(f"{IS_OPEN} && !!document.getElementById('dsky-sun') && !!document.getElementById('dsec-metrics')")
    labels = p.js("[...document.querySelectorAll('#dsec-metrics dt')].map(d => d.textContent)")
    assert "Sunrise" not in labels and "Sunset" not in labels and "Feels like" not in labels
    assert "Feels like" in p.text(".dhero__sub")
    assert "Sunrise" in p.text("#dsky-sun")
    p.screenshot(str(artifacts / f"detail-weather-norepeat-{w}x{h}.png"))
    assert p.errors == [], p.errors


def test_weather_detail_keeps_sunrise_when_there_is_no_sun_card(chrome, srv):
    payload = {**weather_payload(0, 12), "astronomy": None}
    p = dashboard(chrome, srv, weather=payload, at=at_hour(12))
    p.js("window.AuraDetail.open('weather')")
    p.wait_for(f"{IS_OPEN} && !!document.getElementById('dsec-metrics')")
    labels = p.js("[...document.querySelectorAll('#dsec-metrics dt')].map(d => d.textContent)")
    assert "Sunrise" in labels and "Sunset" in labels
    assert not p.js("!!document.getElementById('dsky-sun')")
