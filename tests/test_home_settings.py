"""Settings → Home & rooms: validation, defaults, persistence and /api/config exposure."""

from conftest import make_client

TILES = ["weather", "commute", "advice", "groceries", "calendar", "assistant", "protection", "system"]


def lan(tmp_path):
    return make_client(tmp_path, edit_access="lan")


def test_defaults_exposed_in_config(tmp_path):
    home = lan(tmp_path).get("/api/config").json()["home"]
    assert home["name"] == "Home" and home["tile_order"] == TILES and home["hidden_tiles"] == []
    assert [r["id"] for r in home["rooms"]] == ["entrance", "kitchen", "network"]
    # Rooms only group information tiles; nothing implies connected hardware.
    assert all(set(r["tiles"]) <= set(TILES) for r in home["rooms"])


def test_update_rooms_and_tiles(tmp_path):
    c = lan(tmp_path)
    r = c.patch("/api/settings/home", json={
        "name": "  Sharma   home ",
        "tile_order": ["groceries", "weather"],          # missing tiles are appended in default order
        "hidden_tiles": ["system", "system"],
        "rooms": [{"id": "living-room", "name": "Living room", "tiles": ["weather", "calendar", "weather"]}],
    })
    assert r.status_code == 200, r.text
    v = r.json()["values"]
    assert v["name"] == "Sharma home"
    assert v["tile_order"][:2] == ["groceries", "weather"] and sorted(v["tile_order"]) == sorted(TILES)
    assert v["hidden_tiles"] == ["system"]
    assert v["rooms"] == [{"id": "living-room", "name": "Living room", "tiles": ["weather", "calendar"]}]
    # persisted and served to dashboards
    c2 = lan(tmp_path)
    assert c2.get("/api/config").json()["home"]["rooms"][0]["name"] == "Living room"


def test_invalid_input_rejected(tmp_path):
    c = lan(tmp_path)
    bad = [
        {"name": ""},
        {"name": "x" * 31},
        {"hidden_tiles": ["lights"]},                              # no fake devices
        {"tile_order": ["thermostat"]},
        {"rooms": [{"id": "Bad Id", "name": "A", "tiles": []}]},
        {"rooms": [{"id": "overview", "name": "Overview", "tiles": []}]},
        {"rooms": [{"id": "a", "name": "Same", "tiles": []}, {"id": "b", "name": "same", "tiles": []}]},
        {"rooms": [{"id": f"r{i}", "name": f"Room {i}", "tiles": []} for i in range(9)]},
        {"rooms": [{"id": "a", "name": "<b>\x07</b>", "tiles": []}]},
        {"unknown": 1},
    ]
    for body in bad:
        r = c.patch("/api/settings/home", json=body)
        assert r.status_code == 422, (body, r.text)
    assert c.get("/api/config").json()["home"]["name"] == "Home"           # nothing saved


def test_reset(tmp_path):
    c = lan(tmp_path)
    c.patch("/api/settings/home", json={"name": "Flat 4B", "rooms": []})
    assert c.post("/api/settings/home/reset").json()["values"]["name"] == "Home"
    assert len(c.get("/api/config").json()["home"]["rooms"]) == 3


def test_editing_needs_edit_access(tmp_path):
    c = make_client(tmp_path, client_ip="192.168.1.50")       # pin mode, not unlocked
    assert c.patch("/api/settings/home", json={"name": "Nope"}).status_code == 401
