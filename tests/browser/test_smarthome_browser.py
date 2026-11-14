"""Smart home in the browser: honest "not connected" state, the summary tile, devices by
room, capability-based controls, and command results only after confirmation.
Home Assistant isn't running in tests: the connected states are fixtures."""

import pytest

from fixtures import calendar_payload, commute_payload, weather_payload

pytestmark = pytest.mark.browser
IS_OPEN = "document.getElementById('detail').open"


def dev(eid, name, kind, caps, state, room=None, availability="online", favorite=False, **attrs):
    return {"id": eid, "entity_id": eid, "name": name, "kind": kind, "capabilities": caps, "room": room,
            "room_name": {"kitchen": "Kitchen", "entrance": "Entrance"}.get(room), "favorite": favorite,
            "availability": availability, "state": state, "attributes": attrs, "last_updated": "2026-10-25T08:00:00+00:00",
            "added_at": "2026-10-25T07:00:00+00:00", "integration": "Home Assistant",
            "details": {"platform": "wiz", "manufacturer": "WiZ", "model": "A60", "device_name": name, "hidden": False,
                        "disabled": False, "entity_category": None}, "sensitive_actions": []}


LIGHT = dev("light.kitchen", "Kitchen light", "light", ["on_off", "brightness", "color_temp"], "on", "kitchen", favorite=True,
            brightness=128, color_temp_kelvin=3000, min_color_temp_kelvin=2700, max_color_temp_kelvin=6500)
PLUG = dev("switch.kettle", "Kettle", "plug", ["on_off"], "off", "kitchen", favorite=True)
LEAK = dev("binary_sensor.sink", "Sink leak", "leak", ["reading"], "off", "kitchen")
ATTIC = dev("light.attic", "Attic light", "light", ["on_off"], "unavailable", None, availability="offline")


def connected(devices=(LIGHT, PLUG, LEAK, ATTIC)):
    devices = list(devices)
    online = sum(d["availability"] == "online" for d in devices)
    offline = [d for d in devices if d["availability"] == "offline"]
    return {
        "connection": {"status": "connected", "message": None, "version": "2026.10.3", "connected_at": "2026-10-25T07:00:00+00:00",
                       "ha_url": "http://192.168.1.11:8123"},
        "counts": {"total": len(devices), "online": online, "offline": len(offline), "unknown": 0},
        "favorites": [d for d in devices if d["favorite"]],
        "rooms": [{"id": "kitchen", "name": "Kitchen", "total": 3, "on": 1, "offline": 0}],
        "unassigned": 1, "scenes": [], "automations": {"total": 0, "on": 0},
        "alerts": [], "warnings": [f"{d['name']} is offline" for d in offline],
        "devices": devices, "scene_details": [], "automation_list": [], "guides": [{"title": "Wi-Fi", "text": "Set up in the app."}],
    }


@pytest.fixture
def srv(make_server):
    return make_server(AURA_EDIT_ACCESS="lan")


def dashboard(chrome, srv, smarthome=None, extra=None, w=1920, h=1080):
    p = chrome.new_page()
    p.viewport(w, h)
    for url, body in (extra or {}).items():               # most specific first
        p.fixture(url, *body) if isinstance(body, tuple) else p.fixture(url, body)
    if smarthome is not None:
        p.fixture("/api/smarthome", smarthome)
    p.fixture("/api/weather", weather_payload(63, 12))
    p.fixture("/api/commute", commute_payload("active"))
    p.fixture("/api/calendar", calendar_payload())
    p.goto(f"{srv.url}/")
    p.wait_for("document.getElementById('smarthome-tile').dataset.state !== 'loading'")
    return p


def open_view(p):
    p.js("document.querySelector('#smarthome-tile .tile-arrow').click()")
    p.wait_for(f"{IS_OPEN} && document.getElementById('detail').dataset.view === 'smarthome' && !!document.querySelector('.sh .mp-tabs')", timeout=15)


def test_not_connected_is_said_plainly(chrome, srv, artifacts):
    p = dashboard(chrome, srv)                              # real server, no Home Assistant token
    assert p.text("#smarthome-status") == "Not connected"
    assert p.js("document.getElementById('smarthome-tile').dataset.state") == "off"
    assert p.js("!document.getElementById('smarthome-tile').hidden")       # on the Overview by default
    open_view(p)
    text = p.text("#detail-body")
    assert "Not connected yet" in text and "connect.sh" in text and "No devices in AURA yet" in text
    p.js("document.querySelector('[data-tab=add]').click()")
    assert p.js("document.getElementById('sh-scan').disabled") is True
    p.screenshot(str(artifacts / "smarthome-not-connected.png"))
    assert p.errors == []


def test_tile_summary_and_devices_by_room(chrome, srv, artifacts):
    p = dashboard(chrome, srv, connected())
    assert p.text("#smarthome-status") == "3 of 4 online"
    assert "Attic light is offline" in p.text("#smarthome-sub")
    assert p.js("[...document.querySelectorAll('.shtile__fav')].map(f => [f.textContent, f.dataset.on])") == [["Kitchen light", "true"], ["Kettle", "false"]]
    assert "1 offline" not in p.text("#status-row")         # the tile already says so: no pill for it
    p.js("document.querySelector('.view-tab[data-view=daily]').click()")
    p.wait_for("document.getElementById('status-row').textContent.includes('1 offline')")   # alert pill, from the same data
    p.js("document.querySelector('.view-tab[data-view=home]').click()")
    open_view(p)
    sections = p.js("[...document.querySelectorAll('.sh .dsec__title')].map(t => t.textContent)")
    assert sections[0].startswith("Kitchen · 3 devices · 1 on") and sections[1].startswith("No room")
    toggles = p.js("[...document.querySelectorAll('.sh-card')].map(c => [c.querySelector('b').textContent, !!c.querySelector('.sh-toggle')])")
    assert toggles == [["Kitchen light", True], ["Kettle", True], ["Sink leak", False], ["Attic light", False]]   # sensor: no switch; offline: no switch
    p.screenshot(str(artifacts / "smarthome-devices.png"))


def test_controls_follow_capabilities(chrome, srv, artifacts):
    p = dashboard(chrome, srv, connected())
    open_view(p)
    p.js("window.AuraSmartHome.openDevice('light.kitchen')")
    p.wait_for("!!document.querySelector('.sh-title')")
    labels = p.js("[...document.querySelectorAll('.sh-ctl__label')].map(l => l.textContent)")
    assert labels == ["Power", "Brightness", "Colour temperature"]
    assert "WiZ" in p.text("#detail-body") and "light.kitchen" in p.text("#detail-body")
    p.screenshot(str(artifacts / "smarthome-light.png"))
    p.js("window.AuraSmartHome.openDevice('switch.kettle')")
    p.wait_for("document.querySelector('.sh-title').textContent === 'Kettle'")
    assert p.js("[...document.querySelectorAll('.sh-ctl__label')].map(l => l.textContent)") == ["Power"]
    assert p.js("document.querySelectorAll('input[type=range]').length") == 0
    p.js("window.AuraSmartHome.openDevice('binary_sensor.sink')")
    p.wait_for("document.querySelector('.sh-title').textContent === 'Sink leak'")
    assert "readings only" in p.text("#detail-body") and "Dry" in p.text("#detail-body")
    p.js("window.AuraSmartHome.openDevice('light.attic')")
    p.wait_for("document.querySelector('.sh-title').textContent === 'Attic light'")
    assert "Why it may be offline" in p.text("#detail-body")


@pytest.mark.parametrize("reply, status, shows", [
    ({"result": "confirmed", "device": {**{k: v for k, v in PLUG.items()}, "state": "on"}, "message": None}, 200, "Kettle: done."),
    ({"result": "accepted", "device": PLUG, "message": "Home Assistant accepted the command, but the device hasn't reported the new state yet."},
     200, "hasn't reported"),
    ({"detail": "The device is offline, so nothing was sent."}, 409, "nothing was sent"),
])
def test_result_shown_only_as_confirmed(chrome, srv, reply, status, shows):
    p = dashboard(chrome, srv, connected(), extra={"/api/smarthome/devices/switch.kettle/command": (reply, status)})
    open_view(p)
    p.js("[...document.querySelectorAll('.sh-card')].find(c => c.textContent.includes('Kettle')).querySelector('.sh-toggle').click()")
    p.wait_for("!document.querySelector('.sh-flash').hidden", timeout=10)
    assert shows in p.text(".sh-flash")
    on = p.js("[...document.querySelectorAll('.sh-card')].find(c => c.textContent.includes('Kettle')).querySelector('.sh-toggle').getAttribute('aria-checked')")
    assert on == ("true" if reply.get("result") == "confirmed" else "false")


@pytest.mark.parametrize("w, h", [(1920, 1080), (1600, 900), (1366, 768), (1280, 720)])
def test_tile_fits_every_entrance_size(chrome, srv, w, h, artifacts):
    p = dashboard(chrome, srv, connected(), w=w, h=h)
    tile = p.js("""(() => { const t = document.getElementById('smarthome-tile').getBoundingClientRect();
        return {bottom: t.bottom, vh: innerHeight, scroll: document.documentElement.scrollHeight > innerHeight + 1,
                clipped: [...document.querySelectorAll('#smarthome-tile .tile__value, #smarthome-tile .tile__title')].some(e => e.getBoundingClientRect().bottom > t.bottom)}; })()""")
    assert tile["bottom"] <= tile["vh"] and not tile["scroll"] and not tile["clipped"], tile
    p.screenshot(str(artifacts / f"smarthome-tile-{w}x{h}.png"))
