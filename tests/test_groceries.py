import asyncio
import json
import threading

import pytest

from app.events import Broker, TooManySubscribers, stream
from app.groceries import (
    MAX_ITEMS,
    DuplicateItem,
    GroceryStore,
    ItemIn,
    ItemPatch,
    VersionConflict,
)
from conftest import make_client

PIN = "482916"
LAN = "192.168.1.50"


def add(client, **body):
    return client.post("/api/groceries", json=body)


# --- create ------------------------------------------------------------------

def test_create_item_with_all_fields(client):
    res = add(client, name="Basmati rice", quantity="5 kg", category="pantry", notes="India Gate brand")
    assert res.status_code == 201
    item = res.json()
    assert item["name"] == "Basmati rice"
    assert item["quantity"] == "5 kg"
    assert item["category"] == "pantry" and item["category_label"] == "Pantry"
    assert item["notes"] == "India Gate brand"
    assert item["purchased"] is False
    assert item["created_at"] and item["version"] == 1


def test_create_normalises_input(client):
    item = add(client, name="  milk   ", quantity=2).json()
    assert item["name"] == "Milk"            # trimmed, capitalised
    assert item["quantity"] == "2"           # numbers accepted as text
    assert item["category"] == "dairy"       # guessed from common items


def test_category_is_remembered(client):
    add(client, name="Ragi flour", category="pantry")
    item_id = client.get("/api/groceries").json()["items"][0]["id"]
    client.delete(f"/api/groceries/{item_id}")
    assert add(client, name="ragi flour").json()["category"] == "pantry"


def test_duplicate_open_item_rejected(client):
    add(client, name="Eggs")
    res = add(client, name="  EGGS ")
    assert res.status_code == 409
    assert res.json()["code"] == "duplicate"
    assert res.json()["item"]["name"] == "Eggs"
    assert len(client.get("/api/groceries").json()["items"]) == 1


def test_can_re_add_after_purchase(client):
    item = add(client, name="Bread").json()
    client.patch(f"/api/groceries/{item['id']}", json={"purchased": True})
    assert add(client, name="Bread").status_code == 201


# --- invalid input -------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {},
    {"name": ""},
    {"name": "   "},
    {"name": "x" * 61},
    {"name": "Milk\x00"},
    {"name": "Milk\x1b[31m"},
    {"name": 42},
    {"name": ["Milk"]},
    {"name": "Milk", "category": "snacks"},
    {"name": "Milk", "quantity": "q" * 21},
    {"name": "Milk", "notes": "n" * 201},
    {"name": "Milk", "quantity": True},
])
def test_invalid_create_rejected(client, body):
    assert add(client, **body).status_code == 422
    assert client.get("/api/groceries").json()["status"] == "empty"


def test_invalid_json_rejected(client):
    res = client.post("/api/groceries", content="not json", headers={"content-type": "application/json"})
    assert res.status_code == 422


def test_invalid_patch_rejected(client):
    item = add(client, name="Tea").json()
    url = f"/api/groceries/{item['id']}"
    assert client.patch(url, json={}).status_code == 422
    assert client.patch(url, json={"version": 1}).status_code == 422
    assert client.patch(url, json={"name": "", "version": 1}).status_code == 422
    assert client.patch(url, json={"name": None, "version": 1}).status_code == 422
    assert client.patch(url, json={"purchased": "yes please"}).status_code == 422
    assert client.patch("/api/groceries/abc", json={"purchased": True}).status_code == 422


def test_list_size_is_capped(tmp_path):
    store = GroceryStore(tmp_path / "x.db")
    store._db.executemany(
        "INSERT INTO items (name, name_key, created_at, updated_at) VALUES (?, ?, 't', 't')",
        [(f"I{i}", f"i{i}") for i in range(MAX_ITEMS)],
    )
    with pytest.raises(Exception, match="full"):
        store.add(ItemIn(name="One more"))


# --- edit --------------------------------------------------------------------

def test_edit_item(client):
    item = add(client, name="Coffee", category="drinks").json()
    res = client.patch(f"/api/groceries/{item['id']}", json={
        "name": "Filter coffee", "quantity": "500 g", "notes": "Medium roast", "category": "pantry",
        "version": item["version"],
    })
    assert res.status_code == 200
    edited = res.json()
    assert (edited["name"], edited["quantity"], edited["notes"], edited["category"]) == \
        ("Filter coffee", "500 g", "Medium roast", "pantry")
    assert edited["version"] == 2


def test_edit_can_clear_optional_fields(client):
    item = add(client, name="Apples", quantity="1 kg", notes="Shimla").json()
    edited = client.patch(f"/api/groceries/{item['id']}",
                          json={"quantity": None, "notes": None, "version": 1}).json()
    assert edited["quantity"] is None and edited["notes"] is None


def test_stale_edit_is_rejected_with_latest_item(client):
    item = add(client, name="Butter").json()
    url = f"/api/groceries/{item['id']}"
    # Phone A edits first.
    assert client.patch(url, json={"quantity": "2", "version": 1}).status_code == 200
    # Phone B still has version 1.
    res = client.patch(url, json={"quantity": "3", "version": 1})
    assert res.status_code == 409
    assert res.json()["code"] == "conflict"
    assert res.json()["item"]["quantity"] == "2"
    assert client.get("/api/groceries").json()["items"][0]["quantity"] == "2"


def test_edit_without_version_is_rejected(client):
    item = add(client, name="Sugar").json()
    res = client.patch(f"/api/groceries/{item['id']}", json={"quantity": "1 kg"})
    assert res.status_code == 409


def test_rename_to_existing_item_is_rejected(client):
    add(client, name="Onions")
    item = add(client, name="Garlic").json()
    res = client.patch(f"/api/groceries/{item['id']}", json={"name": "onions", "version": 1})
    assert res.status_code == 409 and res.json()["code"] == "duplicate"


def test_edit_missing_item_404(client):
    assert client.patch("/api/groceries/999", json={"purchased": True}).status_code == 404


# --- purchase ----------------------------------------------------------------

def test_mark_purchased_and_back(client):
    item = add(client, name="Paneer").json()
    url = f"/api/groceries/{item['id']}"
    bought = client.patch(url, json={"purchased": True}).json()
    assert bought["purchased"] is True and bought["purchased_at"]
    body = client.get("/api/groceries").json()
    assert body["remaining"] == 0
    back = client.patch(url, json={"purchased": False}).json()
    assert back["purchased"] is False and back["purchased_at"] is None


def test_purchase_from_two_phones_is_safe(client):
    item = add(client, name="Curd").json()
    url = f"/api/groceries/{item['id']}"
    first = client.patch(url, json={"purchased": True}).json()
    second = client.patch(url, json={"purchased": True})   # stale view, no version needed
    assert second.status_code == 200
    assert second.json()["version"] == first["version"]     # no-op, nothing changed


def test_unpurchase_blocked_if_readded(client):
    old = add(client, name="Tomatoes").json()
    client.patch(f"/api/groceries/{old['id']}", json={"purchased": True})
    add(client, name="Tomatoes")
    res = client.patch(f"/api/groceries/{old['id']}", json={"purchased": False})
    assert res.status_code == 409


# --- delete ------------------------------------------------------------------

def test_delete_item(client):
    item = add(client, name="Detergent").json()
    assert client.delete(f"/api/groceries/{item['id']}").status_code == 204
    assert client.get("/api/groceries").json()["status"] == "empty"
    assert client.delete(f"/api/groceries/{item['id']}").status_code == 404


def test_delete_with_stale_version_conflicts(client):
    item = add(client, name="Rice").json()
    client.patch(f"/api/groceries/{item['id']}", json={"quantity": "10 kg", "version": 1})
    assert client.delete(f"/api/groceries/{item['id']}?version=1").status_code == 409
    assert client.delete(f"/api/groceries/{item['id']}?version=2").status_code == 204


def test_clear_purchased(client):
    a = add(client, name="Milk").json()
    add(client, name="Eggs")
    client.patch(f"/api/groceries/{a['id']}", json={"purchased": True})
    assert client.post("/api/groceries/clear-purchased").json() == {"deleted": 1}
    assert [i["name"] for i in client.get("/api/groceries").json()["items"]] == ["Eggs"]


# --- list, revision, suggestions -----------------------------------------------

def test_revision_increases_on_every_change(client):
    r0 = client.get("/api/groceries").json()["revision"]
    item = add(client, name="Bananas").json()
    r1 = client.get("/api/groceries").json()["revision"]
    client.patch(f"/api/groceries/{item['id']}", json={"purchased": True})
    r2 = client.get("/api/groceries").json()["revision"]
    assert r0 < r1 < r2


def test_list_order_open_first_then_by_category(client):
    add(client, name="Toothpaste")            # personal
    add(client, name="Milk")                  # dairy
    bought = add(client, name="Apples").json()
    client.patch(f"/api/groceries/{bought['id']}", json={"purchased": True})
    names = [i["name"] for i in client.get("/api/groceries").json()["items"]]
    assert names == ["Milk", "Toothpaste", "Apples"]


def test_suggestions_exclude_items_on_list(client):
    add(client, name="Milk")
    names = [s["name"] for s in client.get("/api/groceries/suggestions").json()]
    assert "Milk" not in names and "Eggs" in names


def test_suggestions_prefer_frequent_items(client):
    for _ in range(3):
        item = add(client, name="Coconut water", category="drinks").json()
        client.delete(f"/api/groceries/{item['id']}")
    assert client.get("/api/groceries/suggestions").json()[0]["name"] == "Coconut water"


# --- persistence & migration -------------------------------------------------------

def test_list_survives_restart(tmp_path):
    first = make_client(tmp_path)
    item = add(first, name="Atta", quantity="10 kg", notes="Aashirvaad").json()
    first.patch(f"/api/groceries/{item['id']}", json={"purchased": True})
    add(first, name="Dal")
    rev = first.get("/api/groceries").json()["revision"]
    first.app.state.groceries.close()

    second = make_client(tmp_path)  # new app, same data directory
    body = second.get("/api/groceries").json()
    by_name = {i["name"]: i for i in body["items"]}
    assert set(by_name) == {"Atta", "Dal"}
    assert by_name["Atta"]["purchased"] is True
    assert by_name["Atta"]["notes"] == "Aashirvaad"
    assert body["revision"] == rev


def test_legacy_json_is_imported_once_and_kept(tmp_path):
    legacy = tmp_path / "groceries.json"
    legacy.write_text(json.dumps({"items": [
        {"id": "milk", "name": "Milk", "quantity": "1 L", "checked": False},
        {"id": "eggs", "name": "Eggs", "checked": True},
        {"id": "bad", "name": ""},
    ]}))
    client = make_client(tmp_path)
    items = client.get("/api/groceries").json()["items"]
    assert {(i["name"], i["quantity"], i["purchased"]) for i in items} == \
        {("Milk", "1 L", False), ("Eggs", None, True)}
    assert not legacy.exists()
    assert (tmp_path / "groceries.json.migrated").exists()      # original kept, not deleted
    client.app.state.groceries.close()

    # A stray groceries.json later is not imported again.
    legacy.write_text(json.dumps({"items": [{"name": "Butter"}]}))
    again = make_client(tmp_path)
    assert len(again.get("/api/groceries").json()["items"]) == 2


def test_corrupt_legacy_json_is_left_alone(tmp_path):
    legacy = tmp_path / "groceries.json"
    legacy.write_text("{broken")
    client = make_client(tmp_path)
    assert client.get("/api/groceries").json()["status"] == "empty"
    assert legacy.read_text() == "{broken"


# --- simultaneous edits ----------------------------------------------------------

def test_concurrent_adds_of_same_item_create_one(tmp_path):
    store = GroceryStore(tmp_path / "c.db")
    results: list[str] = []

    def worker():
        try:
            store.add(ItemIn(name="Milk"))
            results.append("added")
        except DuplicateItem:
            results.append("dup")

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("added") == 1 and results.count("dup") == 9
    assert len(store.load().items) == 1


def test_concurrent_edits_only_one_wins(tmp_path):
    store = GroceryStore(tmp_path / "c.db")
    item = store.add(ItemIn(name="Oil"))
    outcomes: list[str] = []

    def worker(qty):
        try:
            store.update(item.id, ItemPatch(quantity=qty, version=item.version))
            outcomes.append("ok")
        except VersionConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=worker, args=(f"{n} L",)) for n in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("ok") == 1 and outcomes.count("conflict") == 7
    assert store.load().items[0].version == 2


# --- live updates ----------------------------------------------------------------

def test_event_stream_sends_hello_then_changes():
    async def scenario():
        broker = Broker()
        disconnected = False

        async def is_disconnected():
            return disconnected

        gen = stream(broker, is_disconnected, {"groceries": 7}, keepalive=0.05)
        assert (await gen.__anext__()).startswith("retry:")
        assert await gen.__anext__() == 'event: hello\ndata: {"groceries":7}\n\n'
        broker.publish("groceries", {"revision": 8})
        assert await gen.__anext__() == 'event: groceries\ndata: {"revision":8}\n\n'
        assert await gen.__anext__() == ": keepalive\n\n"
        broker.close()
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
        assert broker.count == 0

    asyncio.run(scenario())


def test_broker_limits_connections():
    async def scenario():
        broker = Broker(max_subscribers=2)
        broker.subscribe()
        broker.subscribe()
        with pytest.raises(TooManySubscribers):
            broker.subscribe()

    asyncio.run(scenario())


def test_changes_are_published(client):
    async def scenario():
        broker = client.app.state.broker
        q = broker.subscribe()
        add(client, name="Ghee")
        event, data = q.get_nowait()
        assert event == "groceries"
        assert data["revision"] == client.get("/api/groceries").json()["revision"]

    asyncio.run(scenario())


# --- access control --------------------------------------------------------------

@pytest.fixture
def lan(tmp_path):
    return make_client(tmp_path, client_ip=LAN, household_pin=PIN)


def login(c, pin=PIN):
    return c.post("/api/session", json={"pin": pin})


def test_phone_can_read_but_not_write_without_pin(lan):
    assert lan.get("/api/groceries").status_code == 200
    assert lan.get("/api/session").json() == {"can_edit": False, "method": None, "pin_configured": True}
    assert add(lan, name="Milk").status_code == 401
    assert lan.patch("/api/groceries/1", json={"purchased": True}).status_code == 401
    assert lan.delete("/api/groceries/1").status_code == 401
    assert lan.post("/api/groceries/clear-purchased").status_code == 401
    assert lan.put("/api/location", json={"id": 1}).status_code == 401
    assert lan.post("/api/location/detect").status_code == 401


def test_pin_login_enables_edits(lan):
    assert login(lan, "000000").status_code == 401
    res = login(lan)
    assert res.status_code == 200
    cookie = res.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=" in cookie
    assert add(lan, name="Milk").status_code == 201
    assert lan.get("/api/session").json()["method"] == "pin"


def test_logout(lan):
    login(lan)
    lan.delete("/api/session")
    lan.cookies.clear()
    assert add(lan, name="Milk").status_code == 401


def test_forged_cookie_rejected(lan):
    lan.cookies.set("aura_session", "v1.9999999999.abcd.deadbeef")
    assert add(lan, name="Milk").status_code == 401


def test_changing_pin_signs_devices_out(tmp_path):
    phone = make_client(tmp_path, client_ip=LAN, household_pin=PIN)
    login(phone)
    token = phone.cookies.get("aura_session")
    phone.app.state.groceries.close()
    after = make_client(tmp_path, client_ip=LAN, household_pin="135792")
    after.cookies.set("aura_session", token)
    assert add(after, name="Milk").status_code == 401


def test_session_survives_restart(tmp_path):
    phone = make_client(tmp_path, client_ip=LAN, household_pin=PIN)
    login(phone)
    token = phone.cookies.get("aura_session")
    phone.app.state.groceries.close()
    again = make_client(tmp_path, client_ip=LAN, household_pin=PIN)
    again.cookies.set("aura_session", token)
    assert add(again, name="Milk").status_code == 201


def test_wrong_pin_lockout(lan):
    for _ in range(5):
        assert login(lan, "111111").status_code == 401
    res = login(lan)          # even the right PIN is refused while locked
    assert res.status_code == 429
    assert "Retry-After" in res.headers


def test_no_pin_configured_means_only_local_edits(tmp_path):
    phone = make_client(tmp_path, client_ip=LAN)
    assert phone.get("/api/session").json()["pin_configured"] is False
    assert login(phone).status_code == 503
    assert add(phone, name="Milk").status_code == 401
    assert add(make_client(tmp_path), name="Milk").status_code == 201   # entrance display


def test_lan_mode_lets_home_devices_edit_without_pin(tmp_path):
    phone = make_client(tmp_path, client_ip=LAN, edit_access="lan")
    assert phone.get("/api/session").json()["can_edit"] is True
    assert phone.get("/api/session").json()["method"] == "lan"
    item = add(phone, name="Milk").json()
    assert phone.patch(f"/api/groceries/{item['id']}", json={"purchased": True}).status_code == 200
    assert phone.delete(f"/api/groceries/{item['id']}").status_code == 204
    assert phone.post("/api/session", json={"pin": "whatever"}).json()["can_edit"] is True


def test_lan_mode_still_blocks_outsiders_and_other_sites(tmp_path):
    outsider = make_client(tmp_path, client_ip="8.8.8.8", edit_access="lan")
    assert add(outsider, name="Milk").status_code == 403
    phone = make_client(tmp_path, client_ip=LAN, edit_access="lan")
    res = phone.post("/api/groceries", json={"name": "Milk"}, headers={"origin": "http://evil.example"})
    assert res.status_code == 403


def test_lan_is_the_default(tmp_path):
    from app.config import Settings
    assert Settings(_env_file=None).edit_access == "lan"


def test_localhost_trust_can_be_disabled(tmp_path):
    local = make_client(tmp_path, trust_localhost=False, household_pin=PIN)
    assert add(local, name="Milk").status_code == 401


def test_cross_site_writes_blocked(lan):
    login(lan)
    res = lan.post("/api/groceries", json={"name": "Milk"}, headers={"origin": "http://evil.example"})
    assert res.status_code == 403
    same = lan.post("/api/groceries", json={"name": "Milk"}, headers={"origin": "http://localhost"})
    assert same.status_code == 201


def test_short_pin_rejected_in_settings(tmp_path):
    with pytest.raises(ValueError):
        make_client(tmp_path, household_pin="123")


def test_pin_never_exposed(lan):
    for path in ("/api/session", "/api/config", "/", "/groceries"):
        assert PIN not in lan.get(path).text


# --- QR / share URL ------------------------------------------------------------------

def test_share_url_and_qr(tmp_path):
    c = make_client(tmp_path, public_url="http://192.168.1.11:8080")
    assert c.get("/api/share").json()["url"] == "http://192.168.1.11:8080/groceries"
    res = c.get("/api/share/qr.svg")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/svg+xml")
    assert res.text.startswith("<svg") and "viewBox" in res.text


def test_no_qr_when_bound_to_localhost(tmp_path):
    c = make_client(tmp_path, host="127.0.0.1")
    assert c.get("/api/share").json()["url"] is None
    assert c.get("/api/share/qr.svg").status_code == 404


def test_grocery_page_served(client):
    res = client.get("/groceries")
    assert res.status_code == 200 and "Groceries" in res.text
