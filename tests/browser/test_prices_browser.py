"""Price Tracker on the dashboard: the tile in Home-sized and Price Tracker views, ₹ with Indian
grouping, ▲/▼ changes in ₹ and %, estimates labelled, stale and not-set-up states, the detail
page with a chart of recorded readings only, and a refused manual refresh explained."""

import pytest

from fixtures import calendar_payload, commute_payload, weather_payload

pytestmark = pytest.mark.browser


def item(pid, label, unit, price, change, pct, est=False):
    return {"id": pid, "label": label, "unit": unit, "price": price, "change": change, "change_pct": pct,
            "previous_day": "2026-10-26", "estimate": est, "note": "x"}


def view(status="ok", history=3):
    days = ["2026-10-25", "2026-10-26", "2026-10-27"][-history:] if history else []
    return {"status": status, "message": None if status == "ok" else "Showing the last price from 2026-10-27; metals.dev couldn't be reached.",
            "source": "IBJA (India Bullion and Jewellers Association) via metals.dev", "basis": "Bullion benchmark for India, before GST (3%).",
            "day": "2026-10-27", "fetched_at": "2026-10-27T12:00:03+05:30", "source_time": "x",
            "items": [item("gold24", "24K Gold", "10 g", 141630, 1630, 1.16), item("gold22", "22K Gold", "10 g", 129828, 1494, 1.16, True),
                      item("gold20", "20K Gold", "10 g", 118025, 1358, 1.16, True), item("gold18", "18K Gold", "10 g", 106223, 1223, 1.16, True),
                      item("silver", "Silver", "kg", 194417, -583, -0.3)],
            "history": [{"day": d, "gold24": 140000 + i * 815, "gold22": 128333 + i * 747, "gold20": 116667 + i * 679, "gold18": 105000 + i * 611,
                         "silver": 195000 - i * 291} for i, d in enumerate(days)],
            "schedule": "Once a day at 12:00 India time", "next_update": "2026-10-28T12:00", "requests_this_month": 3, "monthly_cap": 90}


@pytest.fixture
def srv(make_server):
    return make_server(AURA_EDIT_ACCESS="lan")


def dashboard(chrome, srv, prices=None, w=1920, h=1080, extra=None):
    p = chrome.new_page()
    p.viewport(w, h)
    for url, body in (extra or {}).items():
        p.fixture(url, *body) if isinstance(body, tuple) else p.fixture(url, body)
    if prices is not None:
        p.fixture("/api/prices", prices)
    p.fixture("/api/weather", weather_payload(0, 9))
    p.fixture("/api/commute", commute_payload("active"))
    p.fixture("/api/calendar", calendar_payload())
    p.goto(f"{srv.url}/")
    p.wait_for("document.getElementById('prices-tile').dataset.state !== 'loading'", timeout=15)
    return p


def test_not_set_up_is_said_plainly(chrome, srv):
    p = dashboard(chrome, srv)                                # test server: no key
    p.js("document.querySelector('.view-tab[data-view=prices]').click()")
    assert "Not set up" in p.text("#prices-tile") and "metals.dev API key" in p.text("#prices-tile")


def test_price_view_tile_and_detail(chrome, srv, artifacts):
    p = dashboard(chrome, srv, view())
    p.js("document.querySelector('.view-tab[data-view=prices]').click()")
    rows = p.js("[...document.querySelectorAll('#prices-tile .pt-row')].map(r => r.textContent)")
    assert len(rows) == 5
    assert "null" not in p.text("#prices-tile")
    assert rows[0] == "24K Gold/ 10 g₹1,41,630▲ ₹1,630 (+1.16%)"              # Indian grouping, ₹ and % change
    assert "est." in rows[1] and rows[4] == "Silver/ kg₹1,94,417▼ ₹583 (-0.30%)"
    assert p.js("document.querySelector('#prices-tile .pt-row[data-id=silver] .pt-chg').dataset.dir") == "down"
    assert "down ₹583" in p.js("document.querySelector('#prices-tile .pt-row[data-id=silver] .pt-chg').getAttribute('aria-label')")
    p.screenshot(str(artifacts / "prices-view-1920x1080.png"))
    p.js("document.querySelector('#prices-tile .tile-arrow').click()")
    p.wait_for("document.getElementById('detail').open && !!document.querySelector('.pt-chart svg')", timeout=10)
    text = p.text("#detail-body")
    for k in ("IBJA (India Bullion and Jewellers Association) via metals.dev", "Once a day at 12:00", "Estimate: 24K × 22/24",
              "3 daily readings recorded by AURA", "before GST", "not jewellers' quotes", "Requests this month: 3"):
        assert k in text, k
    assert p.js("document.querySelectorAll('.pt-chart circle').length") == 3          # one dot per real reading
    p.js("document.querySelector('.pt-card:nth-child(5)').click()")
    p.wait_for("document.querySelector('.dsec__title').textContent.startsWith('Silver · ₹ per kg')", timeout=5)
    p.screenshot(str(artifacts / "prices-detail.png"))
    assert p.errors == []


def test_one_reading_draws_no_fake_line_and_stale_is_marked(chrome, srv):
    p = dashboard(chrome, srv, view(status="stale", history=1))
    p.js("document.querySelector('.view-tab[data-view=prices]').click()")
    assert p.js("document.getElementById('prices-tile').dataset.state") == "alert"
    assert "Showing the last price from 2026-10-27" in p.text("#prices-tile")
    p.js("document.querySelector('#prices-tile .tile-arrow').click()")
    p.wait_for("document.getElementById('detail').open && !!document.querySelector('.pt-cards')", timeout=10)
    assert p.js("document.querySelectorAll('.pt-chart path').length") == 0 and "History builds up" in p.text("#detail-body")


def test_refused_refresh_is_explained(chrome, srv):
    p = dashboard(chrome, srv, view(), extra={"/api/prices/refresh": ({"detail": "Prices were checked recently; try again in 170 min (the free plan allows about 3 checks a day)."}, 429)})
    p.js("location.hash = '#/prices'")
    p.wait_for("document.getElementById('detail').open && !!document.querySelector('.pt-head .btn')", timeout=10)
    p.js("document.querySelector('.pt-head .btn').click()")
    p.wait_for("document.querySelector('.sh-flash')?.dataset.kind === 'error'", timeout=10)
    assert "try again in 170 min" in p.text(".sh-flash")


@pytest.mark.parametrize("w, h", [(1920, 1080), (1600, 900), (1366, 768), (1280, 720)])
def test_tile_fits_in_both_sizes(chrome, srv, w, h, artifacts):
    srv.api("PATCH", "/api/settings/views", {"views": [
        {"id": "a", "name": "Mixed", "tiles": [{"id": "prices", "size": "w"}, {"id": "weather", "size": "w"}, "calendar", "advice", "assistant", "smarthome"]},
        {"id": "prices", "name": "Price Tracker", "columns": "2", "tiles": [{"id": "prices", "size": "l"}]}]})
    p = dashboard(chrome, srv, view(), w, h)
    for v in ("a", "prices"):
        p.js(f"document.querySelector('.view-tab[data-view={v}]').click()")
        g = p.js("""(() => { const t = document.getElementById('prices-tile').getBoundingClientRect();
          const rows = [...document.querySelectorAll('#prices-tile .pt-row')].map(r => r.getBoundingClientRect());
          return {n: rows.length, clipped: rows.filter(r => r.bottom > t.bottom + 1 || r.right > t.right + 1).length,
                  hscroll: document.documentElement.scrollWidth > innerWidth + 1}; })()""")
        assert g["n"] == 5 and g["clipped"] == 0 and not g["hscroll"], (v, w, h, g)
    if w == 1280:
        p.screenshot(str(artifacts / "prices-view-1280x720.png"))
