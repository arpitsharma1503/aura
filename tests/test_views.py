"""Dashboard views: presets, the widget registry, validation, persistence, reset, migration
from the old Overview/room tabs, and a safe fallback for broken stored settings."""

import json
import sqlite3

import pytest

from app.settings_store import CELLS, PRESET_VIEWS, WIDGETS, HomeSettings, ViewsSettings, views_from_home
from conftest import make_client


def lan(tmp_path):
    return make_client(tmp_path, edit_access="lan")


def cells(view) -> int:
    return sum(CELLS[t["size"] if isinstance(t, dict) else t.size] for t in (view["tiles"] if isinstance(view, dict) else view.tiles))


def test_presets_use_real_widgets_and_fit_the_screen():
    v = ViewsSettings()
    assert [x.id for x in v.views] == ["home", "daily", "smart-home", "system"] and v.default_view == "home"
    for view in v.views:
        assert cells(view) <= 12, view.id                                 # 3 rows x 4 columns on the entrance screen
        assert all(t.id in WIDGETS and t.size in WIDGETS[t.id]["sizes"] for t in view.tiles)
    home = [t.id for t in v.views[0].tiles]
    assert home == ["weather", "commute", "meals", "groceries", "advice", "calendar", "assistant", "smarthome"]   # as before views


def test_config_exposes_views_and_registry(tmp_path):
    cfg = lan(tmp_path).get("/api/config").json()
    assert cfg["views"]["default_view"] == "home" and len(cfg["views"]["views"]) == 4
    assert cfg["widgets"]["weather"] == {"name": "Weather", "sizes": ["w", "l"], "size": "w", "about": "Now, 5-day outlook, Sun & Moon"}
    assert set(cfg["widgets"]) == {"weather", "commute", "meals", "groceries", "advice", "calendar", "assistant", "smarthome",
                                   "protection", "system"}
    assert [r["id"] for r in cfg["home"]["rooms"]] == ["living-room", "bedroom", "kitchen", "entrance"]


def test_create_rename_reorder_duplicate_delete_and_default_persist(tmp_path):
    c = lan(tmp_path)
    views = c.get("/api/config").json()["views"]["views"]
    focus = {"id": "focus", "name": "Focus", "description": "Just the day", "columns": "2",
             "tiles": [{"id": "calendar", "size": "l"}, {"id": "weather", "size": "w"}]}
    copy = {**views[0], "id": "home-copy", "name": "Home copy"}
    new = [views[1], focus, views[0], copy]                                  # reordered, one created, one duplicated, two deleted
    r = c.patch("/api/settings/views", json={"views": new, "default_view": "focus"})
    assert r.status_code == 200, r.text
    again = make_client(tmp_path, edit_access="lan").get("/api/config").json()["views"]   # a restart
    assert [v["id"] for v in again["views"]] == ["daily", "focus", "home", "home-copy"] and again["default_view"] == "focus"
    assert again["views"][1]["tiles"] == [{"id": "calendar", "size": "l"}, {"id": "weather", "size": "w"}]
    renamed = [{**v, "name": "Today"} if v["id"] == "focus" else v for v in again["views"]]
    assert c.patch("/api/settings/views", json={"views": renamed}).status_code == 200
    assert c.get("/api/config").json()["views"]["views"][1]["name"] == "Today"


def test_removing_a_widget_from_one_view_keeps_it_elsewhere_and_its_data(tmp_path):
    c = lan(tmp_path)
    c.post("/api/groceries", json={"name": "Milk"})
    views = c.get("/api/config").json()["views"]["views"]
    views[0]["tiles"] = [t for t in views[0]["tiles"] if t["id"] != "groceries"]
    assert c.patch("/api/settings/views", json={"views": views}).status_code == 200
    v = c.get("/api/config").json()["views"]["views"]
    assert "groceries" not in [t["id"] for t in v[0]["tiles"]] and "groceries" in [t["id"] for t in v[1]["tiles"]]
    assert [i["name"] for i in c.get("/api/groceries").json()["items"]] == ["Milk"]


@pytest.mark.parametrize("change, message", [
    ({"views": []}, "at least 1"),
    ({"views": [{"id": "a", "name": "Same", "tiles": []}, {"id": "b", "name": "same", "tiles": []}]}, "different"),
    ({"views": [{"id": "a", "name": "A", "tiles": [{"id": "toaster"}]}]}, "unknown widget"),
    ({"views": [{"id": "Bad id!", "name": "A", "tiles": []}]}, "pattern"),
    ({"views": [{"id": f"v{i}", "name": f"V{i}", "tiles": []} for i in range(9)]}, "at most 8"),
])
def test_invalid_views_are_refused(tmp_path, change, message):
    r = lan(tmp_path).patch("/api/settings/views", json=change)
    assert r.status_code == 422 and message in json.dumps(r.json()).lower().replace("should match pattern", "pattern")


def test_sizes_fall_back_and_default_follows_deletion():
    v = ViewsSettings(views=[{"id": "x", "name": "X", "tiles": [{"id": "advice", "size": "l"}, "weather", "weather"]}],
                      default_view="gone")
    assert [(t.id, t.size) for t in v.views[0].tiles] == [("advice", "s"), ("weather", "w")]   # no 2x2 tips; one weather
    assert v.default_view == "x"


def test_reset_restores_presets_without_touching_other_sections(tmp_path):
    c = lan(tmp_path)
    c.patch("/api/settings/display", json={"clock_format": "24h"})
    c.patch("/api/settings/views", json={"views": [{"id": "only", "name": "Only", "tiles": ["weather"]}], "default_view": "only"})
    r = c.post("/api/settings/views/reset").json()["values"]
    assert [v["id"] for v in r["views"]] == list(PRESET_VIEWS) and r["default_view"] == "home"
    assert c.get("/api/config").json()["clock_format"] == "24h"


def test_old_tab_arrangement_is_migrated_once(tmp_path):
    home = HomeSettings(tile_order=["groceries", "weather"], hidden_tiles=["system"], overview_hidden=["advice"],
                        rooms=[{"id": "living-room", "name": "Living room", "tiles": ["weather", "calendar", "system"]},
                               {"id": "bedroom", "name": "Bedroom", "tiles": []}])
    views = views_from_home(home)
    assert [v.id for v in views] == ["home", "living-room", "smart-home", "system"]
    assert [t.id for t in views[0].tiles][:2] == ["groceries", "weather"] and "advice" not in [t.id for t in views[0].tiles]
    assert [t.id for t in views[1].tiles] == ["weather", "calendar"]                        # hidden everywhere stays hidden
    # through the store: saved old settings, no views yet -> migrated views served
    c = lan(tmp_path)
    c.patch("/api/settings/home", json={"tile_order": ["groceries", "weather"], "overview_hidden": ["advice"],
                                        "rooms": [{"id": "study", "name": "Study", "tiles": ["calendar"]}]})
    again = make_client(tmp_path, edit_access="lan").get("/api/config").json()["views"]
    assert [v["id"] for v in again["views"]][:2] == ["home", "study"]


def test_broken_stored_views_fall_back_to_a_working_dashboard(tmp_path):
    c = lan(tmp_path)
    c.patch("/api/settings/views", json={"default_view": "daily"})
    db = sqlite3.connect(tmp_path / "data" / "aura.db") if (tmp_path / "data" / "aura.db").exists() else sqlite3.connect(next(tmp_path.rglob("aura.db")))
    db.execute("UPDATE settings SET data = ? WHERE section = 'views'", (json.dumps({"views": "nonsense"}),))
    db.commit()
    db.close()
    v = make_client(tmp_path, edit_access="lan").get("/api/config").json()["views"]
    assert [x["id"] for x in v["views"]] == list(PRESET_VIEWS)


def test_changes_need_edit_access(tmp_path):
    c = make_client(tmp_path, client_ip="192.168.1.50")                       # pin mode, not unlocked
    assert c.patch("/api/settings/views", json={"default_view": "daily"}).status_code == 401
