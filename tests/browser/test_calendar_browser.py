"""Calendar agenda on the dashboard and in the Time & date detail (controlled /api/calendar)."""

import pytest

from fixtures import calendar_payload, commute_payload, weather_payload

pytestmark = pytest.mark.browser

IS_OPEN = "document.getElementById('detail').open"


def dashboard(chrome, srv, w=1920, h=1080, calendar=None):
    p = chrome.new_page()
    p.viewport(w, h)
    p.fixture("/api/weather", weather_payload(63, 12))
    p.fixture("/api/commute", commute_payload("active"))
    p.fixture("/api/calendar", calendar or calendar_payload())
    p.goto(f"{srv.url}/")
    p.wait_for("document.getElementById('grocery-list').dataset.state !== 'loading'"
               " && !document.getElementById('agenda').hidden")
    return p


def test_dashboard_agenda_shows_today_capped(chrome, server, artifacts):
    p = dashboard(chrome, server)
    rows = p.js("[...document.querySelectorAll('#agenda li')].map(li => li.textContent.trim())")
    assert len(rows) == 3                                      # 2 items + "+2 more" (max three rows)
    assert "Rohan's birthday" in rows[0] and "Today" in rows[0] and "Diwali" in rows[1]
    assert "+ 2 more" in rows[-1]
    assert sum("Rohan" in r for r in rows) == 1
    p.screenshot(str(artifacts / "agenda-1920.png"))


def test_agenda_falls_forward_to_next_day_with_items(chrome, server):
    p = dashboard(chrome, server, calendar=calendar_payload(today_items=False))
    text = p.text("#agenda")
    assert "Halloween" in text and "Tomorrow" in text


def test_clock_detail_lists_events_once_with_calendars(chrome, server, artifacts):
    p = dashboard(chrome, server)
    p.js("window.AuraDetail.open('clock')")
    p.wait_for(IS_OPEN)
    text = p.text("#detail-body")
    for term in ("Events, birthdays & holidays", "Rohan's birthday", "Diwali", "Dentist", "Halloween",
                 "Family", "Holidays"):
        assert term in text, term
    assert text.count("Rohan's birthday") == 1 and text.count("Trip to Goa") == 1
    assert "until" in text                                     # multi-day range shown once
    p.screenshot(str(artifacts / "detail-calendar-1920.png"))


@pytest.mark.parametrize("w,h", [(1280, 720), (1366, 768), (1920, 1080), (3840, 2160)])
def test_entrance_fits_with_agenda(chrome, server, artifacts, w, h):
    p = dashboard(chrome, server, w, h)
    p.wait_for("document.fonts.status === 'loaded'")
    over = p.js("({v: document.documentElement.scrollHeight - innerHeight, h: document.documentElement.scrollWidth - innerWidth})")
    assert over["v"] <= 0 and over["h"] <= 0, over
    clipped = p.js("""(() => { const a = document.getElementById('agenda').getBoundingClientRect();
        return a.bottom > innerHeight || a.right > innerWidth; })()""")
    assert not clipped
    p.screenshot(str(artifacts / f"agenda-{w}x{h}.png"))
