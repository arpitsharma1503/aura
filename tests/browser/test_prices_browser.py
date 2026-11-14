"""Price Tracker on the dashboard: two KPI tiles (24K gold per 10 g, silver per kg) with the
day-on-day change in ₹ and % (▲/▼), Indian grouping, stale and not-set-up states, the detail
page with recorded history, no manual refresh, and fit at every entrance size."""

import pytest

from fixtures import calendar_payload, commute_payload, weather_payload

pytestmark = pytest.mark.browser


def item(pid, label, unit, price, prev):
    ch = None if prev is None else price - prev
    return {"id": pid, "label": label, "unit": unit, "price": price, "previous": prev, "change": ch,
            "change_pct": None if prev is None else round(ch / prev * 100, 2), "previous_day": None if prev is None else "2026-10-26"}


def view(status="ok", history=3, first=False):
    days = ["2026-10-25", "2026-10-26", "2026-10-27"][-history:] if history else []
    return {"status": status, "message": None if status == "ok" else "Showing the last price from 2026-10-27; metals.dev couldn't be reached.",
            "source": "IBJA (India Bullion and Jewellers Association) via metals.dev", "basis": "Bullion benchmark for India, before GST (3%).",
            "day": "2026-10-27", "fetched_at": "2026-10-27T12:00:03+05:30", "source_time": "x",
            "items": [item("gold24", "24K Gold", "10 g", 141630, None if first else 140000),
                      item("silver", "Silver", "kg", 194417, None if first else 195000)],
            "history": [{"day": d, "gold24": 140000 + i * 815, "silver": 195000 - i * 291} for i, d in enumerate(days)],
            "gold_purities": [] if first else [
                {"id": "gold22", "karat": "22K", "factor": "0.916", "unit": "10 g", "price": 129733, "previous": 128240, "change": 1493, "change_pct": 1.16},
                {"id": "gold20", "karat": "20K", "factor": "0.8333", "unit": "10 g", "price": 118020, "previous": 116662, "change": 1358, "change_pct": 1.16},
                {"id": "gold18", "karat": "18K", "factor": "0.75", "unit": "10 g", "price": 106223, "previous": 105000, "change": 1223, "change_pct": 1.16},
                {"id": "gold14", "karat": "14K", "factor": "0.5833", "unit": "10 g", "price": 82613, "previous": 81662, "change": 951, "change_pct": 1.16}],
            "schedule": "Once a day at 12:00 India time", "next_update": "2026-10-28T12:00", "requests_this_month": 3, "monthly_cap": 40}


@pytest.fixture
def srv(make_server):
    return make_server(AURA_EDIT_ACCESS="lan")


def dashboard(chrome, srv, prices=None, w=1920, h=1080):
    p = chrome.new_page()
    p.viewport(w, h)
    if prices is not None:
        p.fixture("/api/prices", prices)
    p.fixture("/api/weather", weather_payload(0, 9))
    p.fixture("/api/commute", commute_payload("active"))
    p.fixture("/api/calendar", calendar_payload())
    p.goto(f"{srv.url}/")
    p.wait_for("document.getElementById('prices-tile').dataset.state !== 'loading'", timeout=15)
    return p


def test_tab_order(chrome, srv):
    p = dashboard(chrome, srv, view())
    assert p.js("[...document.querySelectorAll('.view-tab')].map(t => t.textContent)") == \
        ["Home", "Daily planning", "Smart home", "Price Tracker", "System"]


def test_not_set_up_is_said_plainly(chrome, srv):
    p = dashboard(chrome, srv)                                # test server: no key
    p.js("document.querySelector('.view-tab[data-view=prices]').click()")
    assert "Not set up" in p.text("#prices-tile") and "metals.dev API key" in p.text("#prices-tile")


def test_two_kpi_tiles_with_day_on_day_change(chrome, srv, artifacts):
    p = dashboard(chrome, srv, view())
    p.js("document.querySelector('.view-tab[data-view=prices]').click()")
    k = p.js("""[...document.querySelectorAll('#prices-tile .pt-kpi')].map(e => ({id: e.dataset.id,
        label: e.querySelector('.pt-kpi__label').textContent, value: e.querySelector('.pt-kpi__value').textContent,
        dod: e.querySelector('.pt-kpi__dod').textContent, dir: e.querySelector('.pt-kpi__dod').dataset.dir,
        aria: e.querySelector('.pt-kpi__dod').getAttribute('aria-label'), vs: e.querySelector('.pt-kpi__vs').textContent}))""")
    assert [x["id"] for x in k] == ["gold24", "silver"]                                  # only these two
    assert k[0] == {"id": "gold24", "label": "24K Gold · per 10 g", "value": "₹1,41,630", "dod": "▲ ₹1,630 · +1.16%", "dir": "up",
                    "aria": "Up ₹1,630, +1.16% from yesterday", "vs": "vs ₹1,40,000 yesterday (26 Oct)"}
    assert k[1]["label"] == "Silver · per kg" and k[1]["value"] == "₹1,94,417" and k[1]["dod"] == "▼ ₹583 · -0.30%" and k[1]["dir"] == "down"
    rows = p.js("[...document.querySelectorAll('#prices-tile .pt-kpi[data-id=gold24] .pt-purity li')].map(li => li.textContent)")
    assert rows == ["22K₹1,29,733▲ ₹1,493 (+1.16%)", "20K₹1,18,020▲ ₹1,358 (+1.16%)", "18K₹1,06,223▲ ₹1,223 (+1.16%)", "14K₹82,613▲ ₹951 (+1.16%)"]
    assert p.js("document.querySelectorAll('#prices-tile .pt-kpi[data-id=silver] .pt-purity').length") == 0
    p.screenshot(str(artifacts / "prices-view-1920x1080.png"))
    p.js("document.querySelector('#prices-tile .tile-arrow').click()")
    p.wait_for("document.getElementById('detail').open && !!document.querySelector('.pt-chart svg')", timeout=10)
    text = p.text("#detail-body")
    assert "once a day at 12:00" in text and "3 daily readings" in text and "before GST" in text
    assert "Refresh" not in text and p.js("document.querySelectorAll('.pt-kpis--detail .pt-kpi').length") == 2
    assert p.js("document.querySelectorAll('.pt-chart circle').length") == 3
    purity = p.js("[...document.querySelectorAll('.pt-kpis--purity .pt-kpi')].map(e => [e.querySelector('.pt-kpi__label').textContent, e.querySelector('.pt-kpi__value').textContent, e.querySelector('.pt-kpi__dod').textContent])")
    assert purity == [["22K Gold · per 10 g", "₹1,29,733", "▲ ₹1,493 · +1.16%"], ["20K Gold · per 10 g", "₹1,18,020", "▲ ₹1,358 · +1.16%"],
                      ["18K Gold · per 10 g", "₹1,06,223", "▲ ₹1,223 · +1.16%"], ["14K Gold · per 10 g", "₹82,613", "▲ ₹951 · +1.16%"]]
    assert "22K × 0.916, 20K × 0.8333, 18K × 0.75, 14K × 0.5833" in p.text("#detail-body")
    p.screenshot(str(artifacts / "prices-detail-gold.png"))
    p.js("document.querySelector('.pt-kpis--detail .pt-kpi[data-id=silver]').click()")
    p.wait_for("[...document.querySelectorAll('.dsec__title')].some(t => t.textContent.startsWith('Silver · ₹ per kg'))", timeout=5)
    assert p.js("document.querySelectorAll('.pt-kpis--purity').length") == 0                # purities belong to gold
    p.screenshot(str(artifacts / "prices-detail.png"))
    assert p.errors == []


def test_first_reading_and_stale(chrome, srv):
    p = dashboard(chrome, srv, view(status="stale", history=1, first=True))
    p.js("document.querySelector('.view-tab[data-view=prices]').click()")
    assert p.js("document.getElementById('prices-tile').dataset.state") == "alert"
    assert "Showing the last price from 2026-10-27" in p.text("#prices-tile") and "null" not in p.text("#prices-tile")
    assert p.js("[...document.querySelectorAll('#prices-tile .pt-kpi__dod')].map(e => e.textContent)") == ["First reading", "First reading"]
    assert "Day-on-day from tomorrow" in p.text("#prices-tile")


def test_missed_day_shows_no_comparison(chrome, srv):
    """Day-on-day is with yesterday only: after a missed day nothing is compared with an older day."""
    v = view(first=True)
    for i in v["items"]:
        i["missed_day"] = "2026-10-26"
    p = dashboard(chrome, srv, v)
    p.js("document.querySelector('.view-tab[data-view=prices]').click()")
    assert p.js("[...document.querySelectorAll('#prices-tile .pt-kpi__dod')].map(e => e.textContent)") == ["No reading on 26 Oct"] * 2
    assert "Compares with yesterday again tomorrow" in p.text("#prices-tile") and "▲" not in p.text("#prices-tile")


@pytest.mark.parametrize("w, h", [(1920, 1080), (1600, 900), (1366, 768), (1280, 720)])
def test_tiles_fit_in_every_size(chrome, srv, w, h, artifacts):
    srv.api("PATCH", "/api/settings/views", {"views": [
        {"id": "a", "name": "Mixed", "tiles": [{"id": "prices", "size": "w"}, {"id": "weather", "size": "w"}, "calendar", "advice", "assistant", "smarthome"]},
        {"id": "b", "name": "Small", "tiles": [{"id": "prices", "size": "s"}, "calendar", "advice", "assistant"]},
        {"id": "prices", "name": "Price Tracker", "columns": "2", "tiles": [{"id": "prices", "size": "l"}]}]})
    p = dashboard(chrome, srv, view(), w, h)
    for v in ("a", "b", "prices"):
        p.js(f"document.querySelector('.view-tab[data-view={v}]').click()")
        g = p.js("""(() => { const t = document.getElementById('prices-tile').getBoundingClientRect();
          const parts = [...document.querySelectorAll('#prices-tile .pt-kpi__value, #prices-tile .pt-kpi__dod, #prices-tile .pt-purity li')]
            .filter(e => e.offsetParent).map(e => [e, e.getBoundingClientRect(), e.closest('.pt-kpi').getBoundingClientRect()]);
          return {n: parts.length, clipped: parts.filter(([e, r, k]) => r.bottom > Math.min(t.bottom, k.bottom) + 1 || r.right > k.right + 1).map(([e]) => e.textContent),
                  hscroll: document.documentElement.scrollWidth > innerWidth + 1}; })()""")
        assert g["n"] == {"a": 8, "b": 4, "prices": 8}[v] and g["clipped"] == [] and not g["hscroll"], (v, w, h, g)
        p.screenshot(str(artifacts / f"prices-{v}-{w}x{h}.png"))


@pytest.mark.parametrize("w", [360, 390, 430])
def test_phone_layout_never_widens_the_page(chrome, srv, w, artifacts):
    """Seen live on 4 Nov: two big KPIs side by side with a large change (▲ ₹7,773 · +4.41%)
    made the page 467 px wide on a 360 px phone. On phones the KPIs stack."""
    v = view()
    v["items"][1].update(price=184198, previous=176425, change=7773, change_pct=4.41)
    p = chrome.new_page()
    p.viewport(w, 800, mobile=True, scale=2)
    p.fixture("/api/prices", v)
    p.goto(f"{srv.url}/")
    p.wait_for("document.getElementById('prices-tile').dataset.state !== 'loading'", timeout=15)
    p.js("document.querySelector('.view-tab[data-view=prices]').click()")
    p.wait_for("document.querySelectorAll('#prices-tile .pt-kpi').length === 2", timeout=5)
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    inside = """[...document.querySelectorAll('#prices-tile .pt-kpi *')].every(e =>
        e.getBoundingClientRect().right <= document.getElementById('prices-tile').getBoundingClientRect().right + 1)"""
    assert p.js(wide) and p.js(inside)
    p.screenshot(str(artifacts / f"prices-phone-{w}.png"))
    p.js("document.querySelector('#prices-tile .tile-arrow').click()")
    p.wait_for("document.getElementById('detail').open", timeout=5)
    assert p.js(wide) and p.js("""[...document.querySelectorAll('#detail-body .pt-kpi *')].every(e =>
        e.getBoundingClientRect().right <= document.getElementById('detail').getBoundingClientRect().right + 1)""")


def test_large_tile_shows_a_trend_of_recorded_readings(chrome, srv):
    """The large Price Tracker tile fills each card with a trend line of AURA's own readings
    (low/high in words); with a single reading it says the line comes later, never drawing one."""
    srv.api("PATCH", "/api/settings/views", {"views": [{"id": "p", "name": "Prices", "tiles": [{"id": "prices", "size": "l"}]}]})
    p = dashboard(chrome, srv, view())
    p.wait_for("document.querySelectorAll('#prices-tile .pt-spark__svg').length === 2", timeout=10)
    assert "3 daily readings" in p.text("#prices-tile .pt-kpi[data-id=gold24] .pt-spark__note")
    p2 = dashboard(chrome, srv, view(history=1, first=True))
    p2.wait_for("document.querySelectorAll('#prices-tile .pt-spark--empty').length === 2", timeout=10)
    assert p2.js("document.querySelectorAll('#prices-tile .pt-spark__svg').length") == 0
