"""Layout checks at every target viewport, for the dashboard and the grocery page."""

import json

import pytest

from fixtures import commute_payload, weather_payload

VIEWPORTS = [
    (320, 568, True), (375, 812, True), (390, 844, True), (430, 932, True),
    (768, 1024, True), (1024, 768, True), (1280, 800, False), (1440, 900, False), (1920, 1080, False),
    # Entrance-display targets, including the 4K TV's real canvas (3840x2160 at scale 1).
    (1280, 720, False), (1366, 768, False), (1600, 900, False), (3840, 2160, False),
]
# Landscape screens >= 1200px wide use the one-screen entrance layout: no scrolling allowed.
ENTRANCE = {(1280, 720), (1280, 800), (1366, 768), (1440, 900), (1600, 900), (1920, 1080), (3840, 2160)}

LOCATION = {"location": {
    "city": "Bengaluru", "region": "Karnataka", "country": "India", "country_code": "IN",
    "latitude": 12.97, "longitude": 77.59, "timezone": "Asia/Kolkata", "source": "ip",
    "confirmed": False, "provider": "ipinfo.io",
    "note": "Services disagree: ipinfo.io says Bengaluru, ipapi.co says Delhi. Please confirm or choose your city.",
    "updated_at": "2026-10-18T00:00:00Z",
}}

LAYOUT_PROBE = """(() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = { hscroll: document.documentElement.scrollWidth - vw, vscroll: document.documentElement.scrollHeight - vh,
                problems: [] };
  const vis = (e) => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const cards = [...document.querySelectorAll(CARD_SELECTOR)].filter(vis);
  for (const c of cards) {
    const r = c.getBoundingClientRect();
    if (r.left < -1 || r.right > vw + 1) out.problems.push(`card ${c.id || c.className} outside viewport (${r.left}..${r.right})`);
  }
  for (let i = 0; i < cards.length; i++) for (let j = i + 1; j < cards.length; j++) {
    const a = cards[i].getBoundingClientRect(), b = cards[j].getBoundingClientRect();
    const w = Math.min(a.right, b.right) - Math.max(a.left, b.left), h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    if (w > 1 && h > 1) out.problems.push(`overlap: ${cards[i].className} / ${cards[j].className}`);
  }
  // Text that must never be clipped: it must fit inside its container.
  for (const sel of TEXT_SELECTORS) for (const e of document.querySelectorAll(sel)) {
    if (!vis(e)) continue;
    const r = e.getBoundingClientRect();
    const box = (e.closest('.card, section, header, .g-app, .g-head') || document.body).getBoundingClientRect();
    // Right edge must stay in its card; left edge only matters at the screen edge
    // (the large clock deliberately overhangs its own padding by ~0.04em).
    if (r.right > box.right + 1 || r.left < -1) out.problems.push(`clipped: ${sel} (${Math.round(r.right)} > ${Math.round(box.right)})`);
    if (e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).textOverflow !== 'ellipsis')
      out.problems.push(`overflowing text: ${sel}`);
  }
  // Touch targets on touch devices.
  if (matchMedia('(pointer: coarse)').matches) {
    for (const e of document.querySelectorAll('button, a[href], input, select, textarea, [role=tab]')) {
      if (!vis(e) || e.closest('dialog:not([open])') || e.disabled) continue;
      const r = e.getBoundingClientRect();
      if (r.height < 40 && r.width < 40) out.problems.push(`small target ${e.id || e.className || e.tagName} ${Math.round(r.width)}x${Math.round(r.height)}`);
      else if (Math.min(r.height, r.width) < 32) out.problems.push(`thin target ${e.id || e.className || e.tagName} ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
  }
  return out;
})()"""


def probe(page, cards: str, texts: list[str]):
    js = LAYOUT_PROBE.replace("CARD_SELECTOR", json.dumps(cards)).replace("TEXT_SELECTORS", json.dumps(texts))
    return page.js(js)


@pytest.fixture(scope="module")
def seeded(chrome, tmp_path_factory):
    from browser.conftest import AuraServer

    d = tmp_path_factory.mktemp("responsive")
    (d / "location.json").write_text(json.dumps(LOCATION))
    srv = AuraServer(d)
    items = [("Basmati rice", "5 kg", "pantry", "India Gate, the long-grain one"),
             ("Milk", "2 L", "dairy", None), ("Tomatoes", "1 kg", "produce", None),
             ("Extra-long item name to check wrapping behaviour", "12 pcs", "household", "Notes that are long enough to wrap"),
             ("Coffee", None, "drinks", None), ("Toothpaste", None, "personal", None)]
    ids = []
    for name, qty, cat, notes in items:
        status, body = srv.api("POST", "/api/groceries", {"name": name, "quantity": qty, "category": cat, "notes": notes})
        assert status == 201, body
        ids.append(body["id"])
    srv.api("PATCH", f"/api/groceries/{ids[2]}", {"purchased": True})
    yield srv
    srv.stop()


@pytest.mark.parametrize("w, h, touch", VIEWPORTS, ids=[f"{w}x{h}" for w, h, _ in VIEWPORTS])
def test_dashboard_layout(chrome, seeded, artifacts, w, h, touch):
    page = chrome.new_page()
    page.viewport(w, h, mobile=touch)
    page.fixture("/api/weather", weather_payload(63, 12))
    page.fixture("/api/commute", commute_payload("active"))
    page.goto(f"{seeded.url}/")
    page.wait_for("document.getElementById('weather').dataset.state === 'ok'"
                  " && document.getElementById('commute').dataset.state === 'active'"
                  " && document.getElementById('grocery-list').dataset.state === 'ok'")
    page.js("new Promise(r => setTimeout(r, 300))")
    result = probe(page, ".card, .time, .topbar", [
        "#clock-hm", "#clock-ampm", "#date", ".place__city", "#weather-temp", "#weather-desc", ".weather__stats dd",
        ".commute__time", ".commute__facts", ".card__title", ".daily li", ".hourly li", ".basis",
    ])
    page.screenshot(str(artifacts / f"dashboard-{w}x{h}.png"))
    assert result["hscroll"] <= 0, f"horizontal scroll {result['hscroll']}px"
    assert result["problems"] == [], result["problems"]
    # Weather animation stays inside its panel at every size.
    assert page.js("""(() => { const c = document.getElementById('weather').getBoundingClientRect(),
        s = document.getElementById('wx-scene').getBoundingClientRect();
        return s.left >= c.left - 1 && s.right <= c.right + 1 && s.top >= c.top - 1 && s.bottom <= c.bottom + 1; })()""")
    if (w, h) in ENTRANCE:
        assert result["vscroll"] <= 0, f"entrance display needs scrolling ({result['vscroll']}px)"
        # Balanced use of space: both content rows get a real share of the screen,
        # the bottom cards aren't squeezed into a strip, and no big empty band
        # sits between the rows.
        rows = page.js("""(() => { const t = document.getElementById('weather').getBoundingClientRect(),
            b = document.getElementById('commute').getBoundingClientRect();
            return {top: t.height / innerHeight, bottom: b.height / innerHeight, gap: (b.top - t.bottom) / innerHeight}; })()""")
        assert 0.42 <= rows["top"] <= 0.62, rows
        assert rows["bottom"] >= 0.3, rows
        assert rows["gap"] <= 0.04, rows
        # Scales with the screen: the clock keeps its share of the width (TV = 2x 1080p).
        clock_share = page.js("document.getElementById('clock').getBoundingClientRect().width / innerWidth")
        assert clock_share >= 0.25, clock_share
    # Location settings dialog fits on screen and is usable.
    page.click("#location-btn")
    page.wait_for("document.getElementById('location-dialog').open")
    fits = page.js("""(() => { const r = document.querySelector('#location-dialog').getBoundingClientRect();
        return r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight + 1; })()""")
    assert fits
    page.js("document.getElementById('location-dialog').close()")
    assert page.errors == [], page.errors


@pytest.mark.parametrize("w, h, touch", VIEWPORTS, ids=[f"{w}x{h}" for w, h, _ in VIEWPORTS])
def test_grocery_page_layout(chrome, seeded, artifacts, w, h, touch):
    page = chrome.new_page()
    page.viewport(w, h, mobile=touch, scale=2 if touch else 1)
    page.goto(f"{seeded.url}/groceries")
    page.wait_for("document.querySelectorAll('.g-item').length >= 5")
    result = probe(page, ".g-items, .g-filter, .g-head", [".g-title h1", ".g-sub", ".g-filter__btn", ".g-group__title"])
    page.screenshot(str(artifacts / f"groceries-{w}x{h}.png"))
    assert result["hscroll"] <= 0, f"horizontal scroll {result['hscroll']}px"
    assert result["problems"] == [], result["problems"]
    # Composer is fully on screen and the add button is reachable.
    assert page.js("""(() => { const r = document.getElementById('g-add').getBoundingClientRect();
        return r.bottom <= innerHeight && r.right <= innerWidth && r.height >= 44; })()""")
    # Edit sheet and confirm dialog fit the screen.
    page.click(".g-body")
    page.wait_for("document.getElementById('g-edit').open")
    assert page.js("""(() => { const r = document.getElementById('g-edit').getBoundingClientRect();
        return r.left >= 0 && r.right <= innerWidth + 0.5 && r.top >= 0 && r.bottom <= innerHeight + 1; })()""")
    page.click("#e-delete")
    page.wait_for("document.getElementById('g-confirm').open")
    assert page.js("""(() => { const r = document.getElementById('g-confirm').getBoundingClientRect();
        return r.left >= 0 && r.right <= innerWidth + 0.5 && r.bottom <= innerHeight + 1; })()""")
    page.click("#c-cancel")
    page.js("document.getElementById('g-edit').close()")
    assert page.errors == [], page.errors
