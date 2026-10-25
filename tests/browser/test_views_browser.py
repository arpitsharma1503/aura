"""Views on the entrance screen: every preset view at every supported size, layouts with
1-4 columns, large widgets, no overlaps, no clipped titles, no sideways scrolling."""

import pytest

from fixtures import calendar_payload, commute_payload, weather_payload

pytestmark = pytest.mark.browser
SIZES = [(1920, 1080), (1600, 900), (1366, 768), (1280, 720)]

GEOMETRY = """(() => {
  const area = document.getElementById('tiles').getBoundingClientRect();
  const tiles = [...document.querySelectorAll('#tiles [data-tile]:not([hidden])')].map(t => ({id: t.dataset.tile, r: t.getBoundingClientRect(),
     title: t.querySelector('.tile__title')?.getBoundingClientRect()}));
  const overlap = [];
  for (let i = 0; i < tiles.length; i++) for (let j = i + 1; j < tiles.length; j++) {
    const a = tiles[i].r, b = tiles[j].r;
    if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) overlap.push([tiles[i].id, tiles[j].id]);
  }
  return {overlap, n: tiles.length,
    outside: tiles.filter(t => t.r.left < area.left - 1 || t.r.right > area.right + 1).map(t => t.id),
    clippedTitle: tiles.filter(t => t.title && (t.title.bottom > t.r.bottom || t.title.right > t.r.right + 1)).map(t => t.id),
    tooSmall: tiles.filter(t => t.r.height < 90 || t.r.width < 150).map(t => t.id),
    hscroll: document.documentElement.scrollWidth > innerWidth + 1,
    vscrollPage: document.documentElement.scrollHeight > innerHeight + 1,
    areaScrolls: document.getElementById('tiles').scrollHeight > document.getElementById('tiles').clientHeight + 1};
})()"""


@pytest.fixture
def srv(make_server):
    return make_server(AURA_EDIT_ACCESS="lan")


def page(chrome, srv, w, h, view=None):
    p = chrome.new_page()
    p.viewport(w, h)
    p.fixture("/api/weather", weather_payload(63, 12))
    p.fixture("/api/commute", commute_payload("active"))
    p.fixture("/api/calendar", calendar_payload())
    p.goto(f"{srv.url}/")
    p.wait_for("!!document.getElementById('tiles').dataset.view && document.getElementById('weather').dataset.state === 'ok'")
    if view:
        p.js(f"document.querySelector('.view-tab[data-view=\"{view}\"]').click()")
    return p


@pytest.mark.parametrize("w, h", SIZES)
@pytest.mark.parametrize("view", ["home", "daily", "smart-home", "system"])
def test_preset_views_fit_every_size(chrome, srv, artifacts, view, w, h):
    p = page(chrome, srv, w, h, view)
    g = p.js(GEOMETRY)
    assert g["n"] > 0 and g["overlap"] == [] and g["outside"] == [] and g["clippedTitle"] == [], g
    assert not g["hscroll"] and not g["vscrollPage"] and not g["areaScrolls"] and g["tooSmall"] == [], g
    if w in (1920, 1280):
        p.screenshot(str(artifacts / f"view-{view}-{w}x{h}.png"))
    assert p.errors == []


@pytest.mark.parametrize("columns", ["1", "2", "3", "4"])
def test_column_layouts_reflow_and_scroll_inside_when_tall(chrome, srv, artifacts, columns):
    status, body = srv.api("PATCH", "/api/settings/views", {"views": [{"id": "all", "name": "Everything", "columns": columns,
        "tiles": ["weather", "commute", "calendar", "groceries", "smarthome", "system", "protection", "assistant", "advice", "meals"]}]})
    assert status == 200, body
    for w, h in [(1920, 1080), (1280, 720)]:
        p = page(chrome, srv, w, h)
        g = p.js(GEOMETRY)
        assert g["n"] == 10 and g["overlap"] == [] and g["outside"] == [] and not g["hscroll"] and not g["vscrollPage"], g
        assert g["tooSmall"] == [], g                                        # rows keep a readable height; the area scrolls instead
        cols = p.js("getComputedStyle(document.getElementById('tiles')).gridTemplateColumns.split(' ').length")
        assert cols == int(columns)
        if w == 1920:
            p.screenshot(str(artifacts / f"view-columns-{columns}.png"))


def test_resizing_reflows_without_reload(chrome, srv):
    p = page(chrome, srv, 1920, 1080, "daily")
    p.js("window.__same = true")
    for w, h in [(1600, 900), (1366, 768), (1280, 720), (1920, 1080)]:
        p.viewport(w, h)
        g = p.js(GEOMETRY)
        assert g["overlap"] == [] and not g["hscroll"] and not g["vscrollPage"], (w, h, g)
    assert p.js("window.__same") is True                                     # no reload happened


def test_views_editor_round_trip(chrome, srv, artifacts):
    p = chrome.new_page()
    p.viewport(1440, 900)
    p.goto(f"{srv.url}/settings#views")
    p.wait_for("!document.getElementById('s-main').hidden && document.querySelectorAll('#view-editor .s-view').length === 5", timeout=15)
    card = "document.querySelector('#view-editor .s-view[data-id=daily]')"
    p.js(f"""(() => {{ const n = {card}.querySelector('.s-view__name'); n.value = 'Today'; n.dispatchEvent(new Event('input', {{bubbles: true}})); }})()""")
    p.js(f"""(() => {{ const s = {card}.querySelector('.s-view__tiles li[data-id=calendar] select'); s.value = 'w'; s.dispatchEvent(new Event('change')); }})()""")
    p.js(f"{card}.querySelector('.s-view__tiles li[data-id=advice] .s-move:last-child').click()")        # remove a widget
    p.js(f"""(() => {{ const c = {card}; c.querySelector('.s-view__pick').value = 'weather'; c.querySelector('.s-view__add').click(); }})()""")
    p.js(f"{card}.querySelector('input[name=default-view]').click()")                                     # start here
    p.js(f"{card}.querySelector('.s-move').click()")                                                      # move up
    assert "fits the entrance screen" in p.text(f"#view-editor .s-view[data-id=daily] .s-preview__note")
    p.js("[...document.querySelectorAll('#view-editor .s-view[data-id=system] .btn')].find(b => b.textContent === 'Duplicate').click()")
    p.js("[...document.querySelectorAll('#view-editor .s-view[data-id=smart-home] .btn')].find(b => b.textContent === 'Delete').click()")
    p.wait_for("document.getElementById('s-confirm').open")
    p.js("document.getElementById('sc-ok').click()")
    p.js("document.getElementById('view-new').value = 'Fitness'; document.getElementById('view-add').click()")
    p.screenshot(str(artifacts / "settings-views-editor.png"))
    p.js("document.querySelector('#views .s-save').click()")
    p.wait_for("document.querySelector('#views .s-status').dataset.kind === 'ok'", timeout=10)
    status, cfg = srv.api("GET", "/api/config")
    v = cfg["views"]
    assert [x["id"] for x in v["views"]] == ["daily", "home", "system", "system-copy", "prices", "fitness"] and v["default_view"] == "daily"
    daily = v["views"][0]
    assert daily["name"] == "Today" and [t["id"] for t in daily["tiles"]] == ["calendar", "meals", "groceries", "commute", "weather"]
    assert daily["tiles"][0]["size"] == "w" and v["views"][3]["name"] == "System copy" and v["views"][5]["tiles"] == []
    # the dashboard opens on the new default view
    d = chrome.new_page()
    d.viewport(1920, 1080)
    d.goto(f"{srv.url}/")
    d.wait_for("document.querySelectorAll('.view-tab').length === 6")
    assert d.js("document.querySelector('.view-tab[aria-selected=true]').textContent") == "Today"
    # the only view can't be deleted
    for vid in ["home", "system", "system-copy", "prices", "fitness"]:
        p.js(f"[...document.querySelectorAll('#view-editor .s-view[data-id={vid}] .btn')].find(b => b.textContent === 'Delete').click()")
        p.wait_for("document.getElementById('s-confirm').open")
        p.js("document.getElementById('sc-ok').click()")
    p.js("[...document.querySelectorAll('#view-editor .s-view .btn')].find(b => b.textContent === 'Delete').click()")
    assert "Keep at least one view" in p.text("#views .s-status")
    assert p.errors == []


def test_reset_one_view_and_all_views(chrome, srv):
    srv.api("PATCH", "/api/settings/views", {"views": [{"id": "home", "name": "My home", "tiles": ["weather"]}]})
    p = chrome.new_page()
    p.viewport(1440, 900)
    p.goto(f"{srv.url}/settings#views")
    p.wait_for("!document.getElementById('s-main').hidden && document.querySelectorAll('#view-editor .s-view').length === 1", timeout=15)
    p.js("[...document.querySelectorAll('#view-editor .s-view[data-id=home] .btn')].find(b => b.textContent === 'Reset').click()")
    assert p.js("document.querySelectorAll('#view-editor .s-view[data-id=home] .s-view__tiles li').length") == 8
    assert p.js("document.querySelector('#view-editor .s-view[data-id=home] .s-view__name').value") == "My home"   # name kept
    p.js("document.querySelector('#views .s-reset').click()")
    p.wait_for("document.getElementById('s-confirm').open")
    p.js("document.getElementById('sc-ok').click()")
    p.wait_for("document.querySelectorAll('#view-editor .s-view').length === 5", timeout=10)


def test_selection_lens_follows_the_selected_view(chrome, srv):
    p = page(chrome, srv, 1920, 1080)
    lens = """(() => { const l = document.querySelector('.view-indicator').getBoundingClientRect();
      const t = document.querySelector('.view-tab[aria-selected=true]').getBoundingClientRect();
      return Math.abs(l.left - t.left) < 2 && Math.abs(l.width - t.width) < 2; })()"""
    assert p.js(lens)
    p.js("document.querySelector('.view-tab[data-view=system]').click()")
    p.wait_for(lens, timeout=3)                                  # glides over, ends under the new tab
    p.viewport(1280, 720)
    p.wait_for(lens, timeout=3)                                  # and stays put when the screen size changes
    # the decorative layers never take clicks
    assert p.js("getComputedStyle(document.querySelector('.view-indicator')).pointerEvents") == "none"
    assert p.js("getComputedStyle(document.querySelector('.tile'), '::before').pointerEvents") == "none"


@pytest.mark.parametrize("phase", ["night", "day"])
def test_glass_can_see_the_background(chrome, srv, phase):
    """Glass must see the background: an ancestor with opacity < 1, a filter, mask, clip-path or
    blend mode would cut it off (a 'backdrop root'; the night dimming once did exactly that).
    The dashboard itself uses no live blur (glass.css explains why); detail sheets do."""
    p = page(chrome, srv, 1920, 1080)
    p.js(f"document.body.dataset.phase = '{phase}'")
    blockers = p.js("""(() => {
      const out = [];
      for (const g of document.querySelectorAll('.tile:not([hidden]), .views__tabs, .glass-cluster, .status__btn, #detail')) {
        for (let e = g.parentElement; e && e !== document.body; e = e.parentElement) {
          const cs = getComputedStyle(e);
          if (cs.opacity !== '1' || cs.filter !== 'none' || cs.mixBlendMode !== 'normal' || cs.clipPath !== 'none' || (cs.maskImage && cs.maskImage !== 'none'))
            out.push([g.id || g.className, e.id || e.className]);
        }
      }
      return out; })()""")
    assert blockers == []
    assert p.js("getComputedStyle(document.getElementById('detail')).backdropFilter") != "none"   # sheets blur live


@pytest.mark.parametrize("w, h", [(320, 568), (390, 844)])
def test_many_views_never_widen_a_phone_page(chrome, srv, w, h):
    """The tab bar scrolls sideways instead of making the page wider (phones would zoom out)."""
    p = chrome.new_page()
    p.viewport(w, h, mobile=True)
    p.fixture("/api/weather", weather_payload(63, 12))
    p.goto(f"{srv.url}/")
    p.wait_for("document.querySelectorAll('.view-tab').length >= 5", timeout=15)
    assert p.js("[document.documentElement.scrollWidth, innerWidth]") == [w, w]
    assert p.js("document.getElementById('views-tabs').scrollWidth > document.getElementById('views-tabs').clientWidth")
