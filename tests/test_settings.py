"""Settings Center: validation, persistence, live application, authorization, Wi-Fi."""

import asyncio
import json
import logging
import os
import stat
import sys
import textwrap
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.commute import schedule_state, slots
from app.netinfo import NetworkService, split_terse
from conftest import FakeNetwork, make_client, save_location
from test_weather import aq_json, forecast_json

HOME = "13.0087307,77.7381464"
OFFICE = "12.9927608,77.6995722"
IST = ZoneInfo("Asia/Kolkata")


def client_for(tmp_path, **kw):
    kw.setdefault("commute_home", HOME)
    kw.setdefault("commute_office", OFFICE)
    kw.setdefault("edit_access", "lan")
    return make_client(tmp_path, **kw)


def patch(c, section, body):
    return c.patch(f"/api/settings/{section}", json=body)


# --- reading & defaults --------------------------------------------------------------

def test_defaults_come_from_env(tmp_path):
    body = client_for(tmp_path).get("/api/settings").json()
    v = body["values"]
    assert v["locations"]["home"] == {"name": "Home", "latitude": 13.0087307, "longitude": 77.7381464}
    assert v["locations"]["office"] == {"name": "Office", "latitude": 12.9927608, "longitude": 77.6995722}
    assert v["commute"]["window_start"] == "07:00" and v["commute"]["window_end"] == "11:00"
    assert v["commute"]["timezone"] == "Asia/Kolkata" and v["commute"]["slot_minutes"] == 15
    assert v["commute"]["days"] == ["mon", "tue", "wed", "thu", "fri"]
    assert v["display"] == {"clock_format": "12h", "temperature_unit": "celsius",
                            "weather_animations": True, "reduced_motion": "system",
                            "background": "aura", "background_dim": 40, "background_version": ""}
    assert body["customized"] == {k: [] for k in v}


def test_no_secrets_in_settings_or_system_info(tmp_path):
    c = client_for(tmp_path, tomtom_api_key="tt-secret-key", household_pin="246810", ipinfo_token="ip-secret")
    text = c.get("/api/settings").text + c.get("/api/system/info").text + c.get("/api/system/network").text
    for secret in ("tt-secret-key", "246810", "ip-secret"):
        assert secret not in text
    assert c.get("/api/settings").json()["info"]["commute"]["key_configured"] is True


def test_dashboard_apis_still_never_expose_coordinates(tmp_path):
    c = client_for(tmp_path)
    for path in ("/api/config", "/api/commute", "/api/location"):
        text = c.get(path).text
        assert "13.0087" not in text and "77.7381" not in text and "12.9927" not in text


# --- saving, validation, preservation ----------------------------------------------

def test_save_one_field_preserves_the_rest(tmp_path):
    c = client_for(tmp_path)
    r = patch(c, "display", {"clock_format": "24h"})
    assert r.status_code == 200 and r.json()["changed"] == ["clock_format"]
    v = c.get("/api/settings").json()["values"]
    assert v["display"]["clock_format"] == "24h"
    assert v["display"]["temperature_unit"] == "celsius"           # same section untouched
    assert v["commute"]["window_start"] == "07:00"                  # other sections untouched
    assert c.get("/api/config").json()["clock_format"] == "24h"


@pytest.mark.parametrize("section, body, field", [
    ("locations", {"home": {"name": "Home", "latitude": 91, "longitude": 77.7}}, "home.latitude"),
    ("locations", {"office": {"name": "Office", "latitude": 12.9, "longitude": -181}}, "office.longitude"),
    ("locations", {"home": {"name": "", "latitude": 12.9, "longitude": 77.7}}, "home.name"),
    ("locations", {"home": {"name": "Home", "latitude": "north", "longitude": 77.7}}, "home.latitude"),
    ("commute", {"window_start": "11:00", "window_end": "07:00"}, "window_end"),
    ("commute", {"window_start": "7am"}, "window_start"),
    ("commute", {"timezone": "Mars/Olympus"}, "timezone"),
    ("commute", {"slot_minutes": 7}, "slot_minutes"),
    ("commute", {"days": []}, "days"),
    ("commute", {"max_requests_per_day": 5000}, "max_requests_per_day"),
    ("commute", {"preferred_arrival": "25:00"}, "preferred_arrival"),
    ("display", {"clock_format": "36h"}, "clock_format"),
    ("weather", {"refresh_minutes": 0}, "refresh_minutes"),
    ("groceries", {"quick_add": ["Milk", "\x07bell"]}, "quick_add"),
    ("display", {"theme": "light"}, "theme"),                     # unknown setting
])
def test_invalid_values_are_rejected_and_nothing_changes(tmp_path, section, body, field):
    c = client_for(tmp_path)
    before = c.get("/api/settings").json()["values"]
    r = patch(c, section, body)
    assert r.status_code == 422, r.text
    fields = [e["field"] for e in r.json()["detail"]["errors"]]
    assert any(f == field or f.startswith(field) for f in fields), fields
    assert c.get("/api/settings").json()["values"] == before


def test_unknown_section_and_bad_body(tmp_path):
    c = client_for(tmp_path)
    assert patch(c, "secrets", {"x": 1}).status_code == 404
    assert c.patch("/api/settings/display", content="nope", headers={"content-type": "application/json"}).status_code == 422
    assert patch(c, "display", {}).status_code == 422


def test_settings_persist_across_restart(tmp_path):
    c = client_for(tmp_path)
    patch(c, "commute", {"window_start": "07:30", "slot_minutes": 30, "preferred_arrival": "09:30"})
    patch(c, "locations", {"office": {"name": "Studio", "latitude": 12.95, "longitude": 77.64}})
    c.app.state.groceries.close()
    c.app.state.prefs.close()
    again = client_for(tmp_path)
    v = again.get("/api/settings").json()["values"]
    assert v["commute"]["window_start"] == "07:30" and v["commute"]["slot_minutes"] == 30
    assert v["commute"]["preferred_arrival"] == "09:30"
    assert v["locations"]["office"] == {"name": "Studio", "latitude": 12.95, "longitude": 77.64}
    assert again.app.state.commute.cfg.window_start == time(7, 30)       # applied at startup too


def test_reset_restores_env_defaults(tmp_path):
    c = client_for(tmp_path)
    patch(c, "locations", {"home": {"name": "Flat", "latitude": 1, "longitude": 2}})
    r = c.post("/api/settings/locations/reset")
    assert r.json()["values"]["home"] == {"name": "Home", "latitude": 13.0087307, "longitude": 77.7381464}
    assert c.get("/api/settings").json()["customized"]["locations"] == []
    assert c.post("/api/settings/nope/reset").status_code == 404


def test_audit_log_without_coordinates(tmp_path, caplog):
    c = client_for(tmp_path)
    with caplog.at_level(logging.INFO, logger="aura.settings"):
        patch(c, "locations", {"home": {"name": "Home", "latitude": 13.111, "longitude": 77.222}})
        patch(c, "commute", {"window_end": "10:30"})
    text = caplog.text
    assert "locations updated (home changed)" in text
    assert "13.111" not in text and "77.222" not in text
    assert "window_end: '11:00' -> '10:30'" in text


# --- settings actually change backend behaviour ----------------------------------------

def test_commute_schedule_applies_to_the_monitor(tmp_path):
    c = client_for(tmp_path)
    m = c.app.state.commute
    patch(c, "commute", {"window_start": "08:00", "window_end": "10:00", "slot_minutes": 30,
                         "days": ["mon", "wed"], "timezone": "Asia/Dubai"})
    cfg = m.cfg
    assert (cfg.window_start, cfg.window_end, cfg.slot_minutes, cfg.days, cfg.timezone) == \
        (time(8), time(10), 30, ("mon", "wed"), "Asia/Dubai")
    monday = date(2026, 10, 19)
    assert [s.strftime("%H:%M") for s in slots(cfg, monday)] == ["08:00", "08:30", "09:00", "09:30", "10:00"]
    dubai = ZoneInfo("Asia/Dubai")
    assert schedule_state(cfg, datetime(2026, 10, 19, 7, 50, tzinfo=dubai)).status == "prefetch"
    assert schedule_state(cfg, datetime(2026, 10, 20, 9, 0, tzinfo=dubai)).status == "off_day"   # Tuesday now off
    status = c.get("/api/commute").json()
    assert status["days"] == ["mon", "wed"] and status["slot_minutes"] == 30


def test_commute_disable_stops_monitoring(tmp_path):
    c = client_for(tmp_path)
    patch(c, "commute", {"enabled": False})
    assert c.app.state.commute.cfg.enabled is False
    assert c.get("/api/commute").json()["state"] == "disabled"
    patch(c, "commute", {"enabled": True})
    assert c.get("/api/commute").json()["state"] != "disabled"


def test_commute_locations_apply_and_labels_update(tmp_path):
    c = client_for(tmp_path)
    patch(c, "locations", {"office": {"name": "Client site", "latitude": 12.9716, "longitude": 77.5946}})
    cfg = c.app.state.commute.cfg
    assert (cfg.office.lat, cfg.office.lon) == (12.9716, 77.5946)
    assert c.get("/api/commute").json()["office_label"] == "Client site"


def test_missing_coordinates_reported_after_clearing(tmp_path):
    c = client_for(tmp_path, tomtom_api_key="k")
    patch(c, "locations", {"office": None})
    body = c.get("/api/commute").json()
    assert body["state"] == "unconfigured" and "office coordinates" in body["message"]


def test_weather_can_use_exact_home_coordinates(tmp_path):
    save_location(tmp_path, city="Bengaluru", latitude=12.97, longitude=77.59)
    net = FakeNetwork({"api.open-meteo.com": (200, forecast_json()), "air-quality-api.open-meteo.com": (200, aq_json())})
    c = client_for(tmp_path, network=net, weather_provider="open_meteo")
    c.get("/api/weather")
    assert net.calls[0].url.params["latitude"] == "12.97"                    # the city
    patch(c, "weather", {"use_home_coordinates": True})
    c.get("/api/weather")
    forecast = [r for r in net.calls if r.url.host == "api.open-meteo.com"][-1]
    assert (forecast.url.params["latitude"], forecast.url.params["longitude"]) == ("13.01", "77.74")
    assert c.get("/api/weather").json()["data"]["city"] == "Bengaluru"       # name unchanged
    assert c.get("/api/settings").json()["info"]["weather"]["using_home_coordinates"] is True


def test_weather_refresh_interval_applies(tmp_path):
    c = client_for(tmp_path)
    patch(c, "weather", {"refresh_minutes": 30})
    assert c.app.state.weather.refresh_seconds == 1800
    assert c.get("/api/config").json()["weather_refresh_seconds"] == 1800


def test_fahrenheit_and_travel_window_reach_recommendations(tmp_path):
    save_location(tmp_path)
    hot = forecast_json(code=0)
    hot["hourly"]["apparent_temperature"] = [36.0] * 30
    net = FakeNetwork({"api.open-meteo.com": (200, hot), "air-quality-api.open-meteo.com": (200, aq_json())})
    c = client_for(tmp_path, network=net, weather_provider="open_meteo")
    water = lambda: next(r for r in c.get("/api/weather").json()["recommendations_all"] if r["id"] == "water")  # noqa: E731
    assert "36°" in water()["detail"]
    patch(c, "display", {"temperature_unit": "fahrenheit"})
    assert "97°" in water()["detail"]
    patch(c, "weather", {"travel_window_hours": 2})
    assert c.get("/api/weather").json()["travel_window_hours"] == 2


def test_quick_add_list_drives_suggestions(tmp_path):
    c = client_for(tmp_path)
    patch(c, "groceries", {"quick_add": ["oat milk", "Ragi", "Ragi"]})
    assert c.get("/api/settings").json()["values"]["groceries"]["quick_add"] == ["Oat milk", "Ragi"]
    names = [s["name"] for s in c.get("/api/groceries/suggestions").json()]
    assert names == ["Oat milk", "Ragi"]


def test_auto_clear_of_old_purchased_items(tmp_path):
    c = client_for(tmp_path)
    old = c.post("/api/groceries", json={"name": "Old"}).json()
    new = c.post("/api/groceries", json={"name": "New"}).json()
    c.patch(f"/api/groceries/{old['id']}", json={"purchased": True})
    c.patch(f"/api/groceries/{new['id']}", json={"purchased": True})
    db = c.app.state.groceries._db
    db.execute("UPDATE items SET purchased_at = ? WHERE id = ?",
               ((datetime.now(IST) - timedelta(days=10)).isoformat(), old["id"]))
    patch(c, "groceries", {"auto_clear_purchased_days": 7})        # applies immediately
    assert [i["name"] for i in c.get("/api/groceries").json()["items"]] == ["New"]
    assert c.get("/api/config").json()["groceries"]["default_filter"] == "needed"


def test_preferred_arrival_changes_the_recommendation(tmp_path):
    from test_commute import MONDAY, Clock, at, cfg, monitor, tomtom
    clock = Clock(at(MONDAY, 6, 45))
    net = tomtom({"09:15": 20, "08:00": 40, "08:15": 38}, clock=clock)
    m = monitor(tmp_path, net, clock)
    asyncio.run(m.tick())
    assert m.status().recommendation.departure_at == at(MONDAY, 9, 15)      # fastest overall
    import dataclasses
    m.reconfigure(dataclasses.replace(m.cfg, preferred_arrival=time(9, 0)))
    st = m.status()
    assert st.recommendation.arrival_at <= at(MONDAY, 9, 0)
    assert st.arrival_note == "Fastest option that arrives by 9:00 AM."
    m.reconfigure(dataclasses.replace(m.cfg, preferred_arrival=time(7, 10)))
    assert "No remaining departure arrives by 7:10 AM" in m.status().arrival_note


# --- authorization -------------------------------------------------------------------------

def test_pin_mode_protects_settings(tmp_path):
    phone = client_for(tmp_path, client_ip="192.168.1.50", edit_access="pin", household_pin="482916")
    assert phone.get("/api/settings").status_code == 401
    assert patch(phone, "display", {"clock_format": "24h"}).status_code == 401
    assert phone.post("/api/settings/display/reset").status_code == 401
    phone.post("/api/session", json={"pin": "482916"})
    assert patch(phone, "display", {"clock_format": "24h"}).status_code == 200


def test_wifi_changes_need_admin_even_in_lan_mode(tmp_path):
    phone = client_for(tmp_path, client_ip="192.168.1.50")                  # LAN mode: can edit settings...
    assert patch(phone, "display", {"clock_format": "24h"}).status_code == 200
    assert phone.post("/api/system/wifi/scan").status_code == 403          # ...but not Wi-Fi
    assert phone.post("/api/system/wifi/connect", json={"ssid": "x", "confirm": True}).status_code == 403
    assert "entrance display" in phone.post("/api/system/wifi/scan").json()["detail"]
    assert phone.get("/api/system/network").status_code == 200             # status is read-only
    assert phone.get("/api/settings").json()["can_admin"] is False


def test_cross_site_settings_change_blocked(tmp_path):
    c = client_for(tmp_path)
    r = c.patch("/api/settings/display", json={"clock_format": "24h"}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403


def test_wifi_connect_requires_confirmation(tmp_path):
    c = client_for(tmp_path)                                               # loopback = entrance display
    assert c.post("/api/system/wifi/connect", json={"ssid": "Home", "password": "x" * 8}).status_code == 422


def test_settings_page_served(client):
    r = client.get("/settings")
    assert r.status_code == 200 and "Settings" in r.text


def test_system_info(tmp_path):
    info = client_for(tmp_path).get("/api/system/info").json()
    for key in ("hostname", "os", "python", "app_version", "app_started_at", "disk_free_gb", "memory_total_mb", "service"):
        assert key in info
    assert info["python"].startswith("3.")


# --- Wi-Fi management through a fake nmcli ---------------------------------------------

FAKE_NMCLI = textwrap.dedent(r'''
    #!{python}
    import json, os, sys, stat
    st = json.load(open(os.environ["FAKE_NM_STATE"]))
    args = sys.argv[1:]
    log = st.setdefault("calls", [])
    rec = {{"argv": args}}
    if "passwd-file" in args:
        f = args[args.index("passwd-file") + 1]
        rec["secret_mode"] = oct(os.stat(f).st_mode & 0o777)
        rec["secret"] = open(f).read()
    log.append(rec)
    def out(s, rc=0, err=""):
        json.dump(st, open(os.environ["FAKE_NM_STATE"], "w"))
        sys.stdout.write(s); sys.stderr.write(err); sys.exit(rc)
    a = [x for x in args if x not in ("-t",)]
    if a[:1] == ["-f"] and a[2:4] == ["general", "status"]:
        out("connected:full:enabled\n")
    if a[:2] == ["general", "permissions"]:
        p = st.get("perm", "yes")
        out(f"org.freedesktop.NetworkManager.network-control:{{p}}\norg.freedesktop.NetworkManager.settings.modify.system:{{p}}\n")
    if a[:1] == ["-f"] and a[1] == "DEVICE,TYPE":
        out("eth0:ethernet\nwlan0:wifi\n")
    if a[:1] == ["-f"] and a[1] == "DEVICE,TYPE,STATE,CONNECTION":
        out(f"eth0:ethernet:{{st.get('eth', 'connected')}}:wired\nwlan0:wifi:{{st.get('wlan', 'disconnected')}}:{{st.get('wlan_con', '')}}\n")
    if a[:1] == ["-f"] and a[1] == "IP4.ADDRESS":
        out("IP4.ADDRESS[1]:192.168.1.20/24\n")
    if a[:1] == ["-f"] and a[1] == "IN-USE,SSID,SIGNAL":
        out(f"*:{{st.get('wlan_ssid', '')}}:70\n" if st.get("wlan") == "connected" else "")
    if a[:1] == ["-f"] and a[1] == "IN-USE,SSID,SIGNAL,SECURITY,FREQ":
        out(" :Cafe\\:Guest:40::2412 MHz\n :Home Net:90:WPA2:5180 MHz\n :Home Net:60:WPA2:2437 MHz\n :Office:55:WPA2 802.1X:2412 MHz\n :Old:30:WEP:2412 MHz\n")
    if a[:1] == ["-f"] and a[1] == "NAME,TYPE":
        out("".join(f"{{n}}:802-11-wireless\n" for n in st.get("profiles", {{}})))
    if a[:2] == ["-g", "802-11-wireless.ssid"]:
        out(st.get("profiles", {{}}).get(a[-1], "") + "\n")
    if a[:2] == ["connection", "add"]:
        name = a[a.index("con-name") + 1]; ssid = a[a.index("ssid") + 1]
        st.setdefault("profiles", {{}})[name] = ssid
        out(f"Connection '{{name}}' added\n")
    if "connection" in a and "up" in a:
        if st.get("fail"):
            out("", 4, st["fail"])
        st["wlan"] = "connected"; st["wlan_ssid"] = st["profiles"].get(a[a.index("up") + 1], "?")
        out("Connection successfully activated\n")
    if a[:2] == ["connection", "delete"]:
        st.get("profiles", {{}}).pop(a[2], None)
        out("deleted\n")
    out("", 0)
''')


@pytest.fixture
def nm(tmp_path):
    script = tmp_path / "nmcli"
    script.write_text(FAKE_NMCLI.format(python=sys.executable).lstrip())
    script.chmod(0o755)
    state = tmp_path / "nm.json"

    def set_state(**kw):
        s = json.loads(state.read_text()) if state.exists() else {}
        s.update(kw)
        state.write_text(json.dumps(s))

    set_state()
    os.environ["FAKE_NM_STATE"] = str(state)
    svc = NetworkService(nmcli=str(script), runtime_dir=str(tmp_path))
    yield svc, set_state, lambda: json.loads(state.read_text())
    os.environ.pop("FAKE_NM_STATE", None)


def run(coro):
    return asyncio.run(coro)


def test_split_terse_handles_escaped_colons():
    assert split_terse(r" :Cafe\:Guest:40::2412 MHz") == [" ", "Cafe:Guest", "40", "", "2412 MHz"]


def test_status_and_scan(nm):
    svc, set_state, _ = nm
    st = run(svc.status())
    assert st.available and st.can_manage and st.connectivity == "full"
    assert [(i.device, i.state) for i in st.interfaces] == [("eth0", "connected"), ("wlan0", "disconnected")]
    nets = run(svc.scan())
    assert [n.ssid for n in nets] == ["Home Net", "Office", "Cafe:Guest", "Old"]   # by signal; dedup keeps strongest
    home = nets[0]
    assert home.signal == 90 and home.band == "5 GHz" and home.security == "WPA2"
    assert nets[2].open and nets[2].ssid == "Cafe:Guest"


def test_connect_uses_private_password_file_never_argv(nm):
    svc, _, state = nm
    run(svc.scan())
    r = run(svc.connect("Home Net", "correct horse battery"))
    assert r.ok and r.code == "connected", r
    calls = state()["calls"]
    for c in calls:
        assert "correct horse battery" not in " ".join(c["argv"])           # never on a command line
    up = next(c for c in calls if "up" in c["argv"])
    assert up["secret_mode"] == "0o600"
    assert up["secret"] == "802-11-wireless-security.psk:correct horse battery\n"
    assert not list(Path(svc.runtime_dir).glob(".aura-wifi-*"))            # deleted afterwards
    add = next(c for c in calls if c["argv"][:2] == ["connection", "add"])
    assert "wpa-psk" in add["argv"] and "Home Net" in add["argv"]


def test_wrong_password_removes_new_profile(nm):
    svc, set_state, state = nm
    run(svc.scan())
    set_state(fail="Error: Connection activation failed: Secrets were required, but not provided.")
    r = run(svc.connect("Home Net", "wrong-password"))
    assert not r.ok and r.code == "wrong_password"
    assert state().get("profiles", {}) == {}                                # cleaned up -> old network kept


def test_connect_validation(nm):
    svc, _, _ = nm
    assert run(svc.connect("Home Net", "whatever12")).code == "invalid"     # no scan yet
    run(svc.scan())
    assert run(svc.connect("Nope", "whatever12")).code == "invalid"
    assert run(svc.connect("Home Net", "short")).code == "invalid"
    assert run(svc.connect("Home Net", "")).code == "invalid"
    assert run(svc.connect("Home Net", "line\nbreak1")).code == "invalid"
    assert run(svc.connect("Cafe:Guest", "password1")).code == "invalid"    # open network takes no password
    assert "enterprise" in run(svc.connect("Office", "password1")).message
    assert "WEP" in run(svc.connect("Old", "password1")).message


def test_wifi_only_connection_needs_extra_confirmation(nm):
    svc, set_state, _ = nm
    set_state(eth="disconnected", wlan="connected", wlan_ssid="Cafe:Guest", wlan_con="cafe")
    run(svc.scan())
    r = run(svc.connect("Home Net", "correct horse battery"))
    assert r.code == "risky" and not r.ok
    assert run(svc.connect("Home Net", "correct horse battery", allow_disconnect=True)).ok


def test_not_authorized_is_reported(nm):
    svc, set_state, _ = nm
    set_state(perm="auth")
    st = run(svc.status())
    assert not st.can_manage and "desktop auto-login" in st.manage_reason
    run(svc.scan())
    assert run(svc.connect("Home Net", "correct horse battery")).code == "not_authorized"


def test_saved_network_connects_without_password(nm):
    svc, set_state, state = nm
    set_state(profiles={"netplan-wlan0-home": "Home Net"})
    run(svc.scan())
    r = run(svc.connect("Home Net", ""))
    assert r.ok
    assert not any(c["argv"][:2] == ["connection", "add"] for c in state()["calls"])


def test_no_nmcli_available():
    svc = NetworkService(nmcli="")
    st = run(svc.status())
    assert st.available is False and "isn't available" in st.message
