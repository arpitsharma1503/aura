"""Panchang in the Calendar: the Tithi line in the tile (real calculation, served as a fixture
because test servers have no home location), the Panchang panel with other dates, Hindi
rendering, and fit at every entrance size. Without a location the Calendar is unchanged."""

from datetime import date, datetime

import pytest

from app.panchang import panchang
from fixtures import calendar_payload, commute_payload, weather_payload

pytestmark = pytest.mark.browser
IST = "Asia/Kolkata"
AGRA = (27.1767, 78.0081)


def payload(d: date, now: datetime) -> dict:
    p = panchang(d, IST, *AGRA)
    return {**p, "status": "ok", "now": panchang(now.date(), IST, *AGRA, now=now)["now"], "today": now.date().isoformat(), "place": "Agra"}


@pytest.fixture
def srv(make_server):
    return make_server(AURA_EDIT_ACCESS="lan")


def dashboard(chrome, srv, w=1920, h=1080, fixtures=None):
    p = chrome.new_page()
    p.viewport(w, h)
    for url, body in (fixtures or {}).items():
        p.fixture(url, body)
    p.fixture("/api/weather", weather_payload(0, 9))
    p.fixture("/api/commute", commute_payload("active"))
    p.fixture("/api/calendar", calendar_payload())
    p.goto(f"{srv.url}/")
    p.wait_for("document.getElementById('calendar-tile').dataset.state !== 'loading'")
    return p


def daily(p):
    """The calm Home view shows one fact per tile; the full calendar tile (and its Tithi line) is in Daily planning."""
    p.wait_for("document.querySelectorAll('.view-tab').length >= 2")
    p.js("document.querySelector('.view-tab[data-view=daily]').click()")
    p.wait_for("document.getElementById('tiles').dataset.view === 'daily'")


NOW = datetime.fromisoformat("2026-10-25T09:00:00+05:30")
TODAY = payload(date(2026, 10, 25), NOW)


def test_no_location_leaves_the_calendar_as_it_was(chrome, srv):
    p = dashboard(chrome, srv)                              # real server: no location -> "no_location"
    daily(p)
    p.wait_for("window.performance.getEntriesByType('resource').some(r => r.name.includes('/api/panchang'))", timeout=10)
    assert p.js("document.getElementById('panchang').hidden") is True
    assert p.js("document.querySelectorAll('#agenda .agenda__item').length") >= 1
    p.js("document.querySelector('#calendar-tile .tile-arrow').click()")
    p.wait_for("document.getElementById('detail').open && !!document.getElementById('dsec-panchang')", timeout=10)
    assert "Panchang unavailable" in p.text("#dsec-panchang") and "home location" in p.text("#dsec-panchang")


def test_tithi_in_the_tile_and_the_panchang_panel(chrome, srv, artifacts):
    other = payload(date(2026, 10, 26), NOW)
    p = dashboard(chrome, srv, fixtures={"/api/panchang?date=2026-10-26": other, "/api/panchang": TODAY})
    daily(p)
    p.wait_for("!document.getElementById('panchang').hidden", timeout=10)
    assert p.text("#panchang-hi") == "शुक्ल पक्ष · चतुर्दशी"
    assert p.text("#panchang-en").startswith("Shukla Chaturdashi · until 11:5")
    # Devanagari really renders (a font with the glyphs is installed): wider than the same count of boxes would be
    assert p.js("document.getElementById('panchang-hi').getBoundingClientRect().width") > 60
    assert p.js("document.querySelectorAll('#agenda .agenda__item').length") <= 7          # the large tile: one row given to the Tithi
    p.screenshot(str(artifacts / "panchang-tile-1920x1080.png"))
    p.js("document.getElementById('panchang').click()")
    p.wait_for("document.getElementById('detail').open && !!document.querySelector('.pan-hero')", timeout=10)
    text = p.text("#dsec-panchang")
    for k in ("शुक्ल पक्ष — चतुर्दशी", "Shukla Paksha Chaturdashi", "Ashwin", "Vikram Samvat 2083", "Sunrise", "Nakshatra", "Yoga",
              "Karana", "Right now: Shukla Paksha Chaturdashi", "Then Purnima", "Swiss Ephemeris"):
        assert k in text, k
    p.screenshot(str(artifacts / "panchang-detail.png"))
    p.js("document.querySelector('.pan-nav [aria-label=\"Next day\"]').click()")
    p.wait_for("document.querySelector('.pan-nav__date')?.textContent.includes('26 October')", timeout=10)
    p.wait_for("!!document.querySelector('.pan-hero')", timeout=10)
    assert "Purnima" in p.text(".pan-hero") and "पूर्णिमा" in p.text(".pan-hero__badge")
    assert "Right now" not in p.text("#dsec-panchang")                                     # only on today
    assert "Events, birthdays & holidays" in p.text("#detail-body")                          # the calendar is still there
    assert p.errors == []


@pytest.mark.parametrize("w, h", [(1920, 1080), (1600, 900), (1366, 768), (1280, 720)])
def test_tile_still_fits(chrome, srv, w, h, artifacts):
    p = dashboard(chrome, srv, w, h, fixtures={"/api/panchang": TODAY})
    daily(p)
    p.wait_for("!document.getElementById('panchang').hidden", timeout=10)
    g = p.js("""(() => { const t = document.getElementById('calendar-tile').getBoundingClientRect();
      const b = document.getElementById('panchang').getBoundingClientRect();
      const last = [...document.querySelectorAll('#agenda > li')].at(-1)?.getBoundingClientRect();
      return {inside: b.bottom <= t.bottom + 1 && b.top >= t.top, overlap: last ? last.bottom > b.top + 1 : false,
              hscroll: document.documentElement.scrollWidth > innerWidth + 1}; })()""")
    assert g["inside"] and not g["overlap"] and not g["hscroll"], g
    if w == 1280:
        p.screenshot(str(artifacts / "panchang-tile-1280x720.png"))
