"""Sun & Moon on the weather card and in its detail view, in a real browser."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from fixtures import weather_payload
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
        const lit = s.querySelector('clipPath > *'); const b = lit.getBBox(); return b.x + b.width / 2; }})()""")


def test_tile_header_shows_moon_phase(chrome, srv):
    p = dashboard(chrome, srv, weather=weather_payload(0, 21, moon_at=GIBBOUS))
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
    p = dashboard(chrome, srv, weather=weather_payload(0, 21, latitude=latitude, moon_at=when))
    p.wait_for("!!document.querySelector('#weather-moon svg.moon clipPath > *')")
    assert p.js("document.querySelector('#weather-moon svg.moon').dataset.litSide") == side
    cx = lit_center(p, "#weather-moon")
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
    p = dashboard(chrome, srv, weather=weather_payload(0, hour))
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
    p = dashboard(chrome, srv, w, h, weather=weather_payload(0, 21, moon_at=GIBBOUS))
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
