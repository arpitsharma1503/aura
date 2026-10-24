"""Smart home: capabilities from Home Assistant's own feature flags, the registry, the
Home Assistant link (against a simulated Home Assistant WebSocket), commands that are
only reported as done when the device confirms, scenes, and the API's access rules."""

import asyncio
import json

import pytest

from app.smarthome import (Command, CommandError, DeviceIn, DevicePatch, HomeAssistantLink, Registry, SceneIn,
                           SmartHome, describe, service_call)


# --------------------------------------------------------------------------- a simulated Home Assistant

def ent(eid, state, **attrs):
    return {"entity_id": eid, "state": state, "attributes": {"friendly_name": eid.split(".")[1].replace("_", " ").title(), **attrs},
            "last_updated": "2026-10-25T00:00:00+00:00"}


def default_states():
    return [
        ent("light.living_room", "off", supported_color_modes=["color_temp", "hs"], min_color_temp_kelvin=2700, max_color_temp_kelvin=6500),
        ent("light.porch", "off", supported_color_modes=["onoff"]),
        ent("switch.kettle", "off", device_class="outlet"),
        ent("fan.bedroom", "off", supported_features=1 | 16 | 32),
        ent("cover.curtain", "closed", supported_features=1 | 2 | 4 | 8),
        ent("lock.front_door", "locked"),
        ent("sensor.bedroom_temperature", "24.5", device_class="temperature", unit_of_measurement="°C"),
        ent("sensor.wifi_signal", "-60", device_class="signal_strength"),
        ent("binary_sensor.kitchen_leak", "off", device_class="moisture"),
        ent("light.attic", "unavailable", supported_color_modes=["brightness"]),
        ent("automation.porch_light_at_sunset", "on", last_triggered=None),
    ]


class FakeHA:
    """Speaks the Home Assistant WebSocket protocol closely enough for AURA's link."""

    def __init__(self, token="good", states=None, respond=True, flows=None):
        self.token, self.respond = token, respond
        self.states = {s["entity_id"]: s for s in (states or default_states())}
        self.flows = flows or []
        self.calls = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.inbox.put_nowait(json.dumps({"type": "auth_required"}))
        self.subscribed = 0
        self.closed = False

    async def connect(self, url):
        return self

    async def recv(self):
        return await self.inbox.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self.inbox.get()
        if msg is None:
            raise StopAsyncIteration
        return msg

    async def close(self):
        self.closed = True

    def push(self, obj):
        self.inbox.put_nowait(json.dumps(obj))

    def set_state(self, eid, state, **attrs):
        new = {**self.states[eid], "state": state}
        new["attributes"] = {**new["attributes"], **attrs}
        self.states[eid] = new
        self.push({"type": "event", "event": {"event_type": "state_changed", "data": {"entity_id": eid, "new_state": new}}})

    async def send(self, raw):
        m = json.loads(raw)
        if m["type"] == "auth":
            self.push({"type": "auth_ok" if m["access_token"] == self.token else "auth_invalid", "ha_version": "2026.10.3"})
            return
        ok = lambda r=None: self.push({"id": m["id"], "type": "result", "success": True, "result": r})  # noqa: E731
        t = m["type"]
        if t == "subscribe_events":
            self.subscribed += 1
            ok()
        elif t == "get_states":
            ok(list(self.states.values()))
        elif t == "config/entity_registry/list":
            ok([{"entity_id": e, "platform": "tuya", "device_id": "d1" if e == "switch.kettle" else None,
                 "hidden_by": None, "disabled_by": None,
                 "entity_category": "diagnostic" if e == "sensor.wifi_signal" else None} for e in self.states])
        elif t == "config/device_registry/list":
            ok([{"id": "d1", "manufacturer": "Wipro", "model": "Smart Plug 16A", "name": "Kettle plug", "name_by_user": None}])
        elif t == "config_entries/flow/progress":
            ok(self.flows)
        elif t == "call_service":
            self.calls.append((m["domain"], m["service"], m["service_data"], m["target"]["entity_id"]))
            ok({"context": {}})
            if self.respond:
                self._apply(m["domain"], m["service"], m["service_data"], m["target"]["entity_id"])
        else:
            self.push({"id": m["id"], "type": "result", "success": False, "error": {"message": "unknown command"}})

    def _apply(self, domain, service, data, eid):
        if service in ("turn_on", "turn_off"):
            attrs = {}
            if "brightness_pct" in data:
                attrs["brightness"] = round(data["brightness_pct"] * 255 / 100)
            if "color_temp_kelvin" in data:
                attrs["color_temp_kelvin"] = data["color_temp_kelvin"]
            self.set_state(eid, "on" if service == "turn_on" else "off", **attrs)
        elif service == "set_cover_position":
            self.set_state(eid, "open", current_position=data["position"])
        elif service in ("lock", "unlock"):
            self.set_state(eid, "locked" if service == "lock" else "unlocked")


async def connected(fake, tmp_path, rooms=None):
    reg = Registry(tmp_path / "aura.db")
    changes = []
    link = HomeAssistantLink("http://127.0.0.1:8123", "good", on_change=changes.append, connect=fake.connect)
    sh = SmartHome(reg, link, lambda: rooms or [{"id": "living-room", "name": "Living room"}, {"id": "bedroom", "name": "Bedroom"}],
                   lambda: "http://192.168.1.11:8123")
    link.start()
    for _ in range(200):
        if link.status == "connected":
            break
        await asyncio.sleep(0.01)
    return sh, changes


# --------------------------------------------------------------------------- capabilities

@pytest.mark.parametrize("eid, attrs, kind, caps", [
    ("light.a", {"supported_color_modes": ["onoff"]}, "light", ["on_off"]),
    ("light.a", {"supported_color_modes": ["brightness"]}, "light", ["on_off", "brightness"]),
    ("light.a", {"supported_color_modes": ["color_temp", "hs"]}, "light", ["on_off", "brightness", "color_temp", "color"]),
    ("switch.a", {"device_class": "outlet"}, "plug", ["on_off"]),
    ("switch.a", {}, "switch", ["on_off"]),
    ("fan.a", {"supported_features": 1 | 16 | 32}, "fan", ["on_off", "fan_speed"]),
    ("cover.a", {"supported_features": 1 | 2}, "cover", ["open_close"]),
    ("cover.a", {"supported_features": 1 | 2 | 4 | 8}, "cover", ["open_close", "position", "stop"]),
    ("climate.a", {"supported_features": 1, "hvac_modes": ["off", "cool"], "current_temperature": 27}, "climate",
     ["hvac_mode", "target_temperature", "reading"]),
    ("media_player.tv", {"supported_features": 128 | 256 | 4}, "media", ["on_off", "volume"]),
    ("lock.a", {}, "lock", ["lock"]),
    ("binary_sensor.a", {"device_class": "moisture"}, "leak", ["reading"]),
    ("sensor.a", {"device_class": "temperature"}, "sensor", ["reading"]),
])
def test_only_reported_capabilities(eid, attrs, kind, caps):
    assert describe(eid, attrs) == (kind, caps)


def test_unsupported_things_are_left_out():
    assert describe("sensor.a", {"device_class": "signal_strength"}) is None
    assert describe("binary_sensor.a", {"device_class": "update"}) is None
    assert describe("vacuum.a", {}) is None


def test_values_are_range_checked():
    with pytest.raises(CommandError):
        service_call("light.a", {}, "set_brightness", 0)
    with pytest.raises(CommandError):
        service_call("light.a", {"min_color_temp_kelvin": 2700, "max_color_temp_kelvin": 6500}, "set_color_temp", 9000)
    with pytest.raises(CommandError):
        service_call("climate.a", {"hvac_modes": ["off", "cool"]}, "set_hvac_mode", "heat")
    assert service_call("light.a", {}, "set_brightness", 30)[:3] == ("light", "turn_on", {"brightness_pct": 30})


# --------------------------------------------------------------------------- link, registry, commands

def test_not_configured_without_a_token(tmp_path):
    link = HomeAssistantLink("http://127.0.0.1:8123", None)
    sh = SmartHome(Registry(tmp_path / "aura.db"), link, lambda: [], lambda: None)
    s = sh.summary()
    assert s["connection"]["status"] == "not_configured" and s["counts"]["total"] == 0
    assert "isn't connected" in s["connection"]["message"]


def test_wrong_token_is_reported(tmp_path):
    async def go():
        fake = FakeHA(token="other")
        link = HomeAssistantLink("http://x:8123", "good", connect=fake.connect)
        link.start()
        for _ in range(200):
            if link.status == "auth_failed":
                break
            await asyncio.sleep(0.01)
        await link.stop()
        return link
    link = asyncio.run(go())
    assert link.status == "auth_failed" and "token" in link.message


def test_discovery_lists_real_entities_and_hides_diagnostics(tmp_path):
    async def go():
        fake = FakeHA(flows=[{"flow_id": "f1", "handler": "matter", "context": {"source": "zeroconf", "title_placeholders": {"name": "Smart bulb"}}},
                             {"flow_id": "f2", "handler": "x", "context": {"source": "user"}}])
        sh, _ = await connected(fake, tmp_path)
        cands = {c["entity_id"]: c for c in sh.candidates()}
        found = await sh.discovered()
        await sh.link.stop()
        return cands, found
    cands, found = asyncio.run(go())
    assert "sensor.wifi_signal" not in cands and "automation.porch_light_at_sunset" not in cands
    assert cands["switch.kettle"]["manufacturer"] == "Wipro" and cands["switch.kettle"]["model"] == "Smart Plug 16A"
    assert cands["light.attic"]["availability"] == "offline"
    assert found == [{"flow_id": "f1", "integration": "matter", "name": "Smart bulb", "found_by": "zeroconf"}]


def test_add_rename_room_favorite_and_views(tmp_path):
    async def go():
        sh, _ = await connected(FakeHA(), tmp_path)
        v = sh.add(DeviceIn(entity_id="light.living_room", room="living-room"))
        with pytest.raises(CommandError) as dup:
            sh.add(DeviceIn(entity_id="light.living_room"))
        with pytest.raises(CommandError) as missing:
            sh.add(DeviceIn(entity_id="light.nowhere"))
        with pytest.raises(CommandError) as bad_room:
            sh.add(DeviceIn(entity_id="light.porch", room="garage"))
        sh.update("light.living_room", DevicePatch(name="Ceiling light", favorite=True))
        sh.add(DeviceIn(entity_id="light.attic"))
        s = sh.summary()
        await sh.link.stop()
        return v, dup.value.status, missing.value.status, bad_room.value, s
    v, dup, missing, bad_room, s = asyncio.run(go())
    assert v["capabilities"] == ["on_off", "brightness", "color_temp", "color"] and v["room_name"] == "Living room"
    assert dup == 409 and missing == 404 and "Unknown room" in str(bad_room)
    assert s["counts"] == {"total": 2, "online": 1, "offline": 1, "unknown": 0}
    assert [f["name"] for f in s["favorites"]] == ["Ceiling light"]
    assert s["warnings"] == ["Attic is offline"] and s["rooms"][0]["name"] == "Living room" and s["unassigned"] == 1


def test_command_is_confirmed_only_by_the_device_state(tmp_path):
    async def go():
        fake = FakeHA()
        sh, _ = await connected(fake, tmp_path)
        sh.add(DeviceIn(entity_id="light.living_room"))
        r = await sh.command("light.living_room", Command(action="set_brightness", value=30, request_id="req-00000001"))
        again = await sh.command("light.living_room", Command(action="set_brightness", value=30, request_id="req-00000001"))
        fake.respond = False                                 # the bulb stops reporting back
        silent = await sh.command("light.living_room", Command(action="turn_off", request_id="req-00000002"))
        await sh.link.stop()
        return r, again, silent, fake.calls
    r, again, silent, calls = asyncio.run(go())
    assert r["result"] == "confirmed" and r["device"]["state"] == "on"
    assert again["repeat"] is True and len(calls) == 2              # the replay didn't reach Home Assistant
    assert silent["result"] == "accepted" and "hasn't reported" in silent["message"]


def test_commands_need_the_capability_and_an_online_device(tmp_path):
    async def go():
        sh, _ = await connected(FakeHA(), tmp_path)
        for e in ("light.porch", "light.attic", "lock.front_door"):
            sh.add(DeviceIn(entity_id=e))
        errs = {}
        for name, eid, cmd in [
            ("no_dimmer", "light.porch", Command(action="set_brightness", value=50, request_id="r-0000000a")),
            ("offline", "light.attic", Command(action="turn_on", request_id="r-0000000b")),
            ("unconfirmed_unlock", "lock.front_door", Command(action="unlock", request_id="r-0000000c")),
            ("not_added", "switch.kettle", Command(action="turn_on", request_id="r-0000000d")),
        ]:
            with pytest.raises(CommandError) as e:
                await sh.command(eid, cmd)
            errs[name] = e.value.status
        unlocked = await sh.command("lock.front_door", Command(action="unlock", confirm=True, request_id="r-0000000e"))
        await sh.link.stop()
        return errs, unlocked
    errs, unlocked = asyncio.run(go())
    assert errs == {"no_dimmer": 422, "offline": 409, "unconfirmed_unlock": 428, "not_added": 404}
    assert unlocked["result"] == "confirmed"


def test_scene_touches_only_its_own_devices_and_never_unlocks(tmp_path):
    async def go():
        fake = FakeHA()
        sh, _ = await connected(fake, tmp_path)
        for e in ("light.living_room", "switch.kettle", "cover.curtain", "light.porch"):
            sh.add(DeviceIn(entity_id=e))
        scene = SceneIn(name="Movie Time", actions=[{"entity_id": "light.living_room", "action": "set_brightness", "value": 20},
                                                    {"entity_id": "cover.curtain", "action": "close"}])
        sh.scene_check(scene)
        sid = sh.registry.save_scene(scene)
        with pytest.raises(CommandError):
            sh.scene_check(SceneIn(name="Bad", actions=[{"entity_id": "light.porch", "action": "set_brightness", "value": 5}]))
        r = await sh.run_scene(sid, "run-00000001")
        await sh.link.stop()
        return r, fake.calls
    r, calls = asyncio.run(go())
    assert {c[3] for c in calls} == {"light.living_room", "cover.curtain"}
    assert r["total"] == 2 and r["results"][0]["result"] == "confirmed"
    with pytest.raises(ValueError):
        SceneIn(name="Open up", actions=[{"entity_id": "lock.front_door", "action": "unlock"}])


def test_live_updates_and_reconnect_resubscribe(tmp_path):
    async def go():
        fake = FakeHA()
        sh, changes = await connected(fake, tmp_path)
        fake.set_state("binary_sensor.kitchen_leak", "on")
        await asyncio.sleep(0.05)
        sh.add(DeviceIn(entity_id="binary_sensor.kitchen_leak"))
        alerts = sh.summary()["alerts"]
        fake.inbox.put_nowait(None)                         # Home Assistant restarts
        for _ in range(100):
            if sh.link.status != "connected":
                break
            await asyncio.sleep(0.01)
        status_after_drop = sh.link.status
        await sh.link.stop()
        return changes, alerts, status_after_drop, fake.subscribed
    changes, alerts, status_after_drop, subscribed = asyncio.run(go())
    assert {"binary_sensor.kitchen_leak"} in changes
    assert alerts == ["Kitchen Leak: water detected"]
    assert status_after_drop in ("connecting", "unreachable") and subscribed == 1


# --------------------------------------------------------------------------- voice

from app.smarthome import VOICE_PATTERNS, voice_command  # noqa: E402


def _voice(tmp_path, *qs, can_control=True, setup=None):
    async def go():
        fake = FakeHA()
        sh, _ = await connected(fake, tmp_path)
        sh.add(DeviceIn(entity_id="light.living_room", name="Ceiling light", room="living-room"))
        sh.add(DeviceIn(entity_id="light.porch", name="Porch light"))
        sh.add(DeviceIn(entity_id="switch.kettle", name="Kettle", room="living-room"))
        sh.add(DeviceIn(entity_id="lock.front_door", name="Front door"))
        sh.add(DeviceIn(entity_id="light.attic", name="Attic light", room="bedroom"))
        sh.registry.save_scene(SceneIn(name="Movie Time", actions=[{"entity_id": "light.living_room", "action": "set_brightness", "value": 20}]))
        out = [await voice_command(sh, q, can_control) for q in qs]
        await sh.link.stop()
        return out, fake.calls
    return asyncio.run(go())


def test_voice_commands_resolve_rooms_names_and_scenes(tmp_path):
    out, calls = _voice(tmp_path, "turn on the living room lights", "set ceiling light to 30 percent", "switch kettle off",
                        "activate movie time", "what devices are offline", "turn on the light")
    texts = [t for t, _ in out]
    assert texts[0] == "Done: Ceiling light is on."                       # only lights in that room, not the kettle
    assert texts[1] == "Done: Ceiling light is at 30%."
    assert texts[2] == "Done: Kettle is off."
    assert texts[3] == "Movie Time: 1 of 1 changes confirmed."
    assert texts[4] == "Offline right now: Attic light."
    assert texts[5].startswith("Which one:")                              # ambiguous: nothing done
    assert [c[3] for c in calls] == ["light.living_room", "light.living_room", "switch.kettle", "light.living_room"]


def test_voice_never_unlocks_and_respects_permission(tmp_path):
    out, calls = _voice(tmp_path, "unlock the front door")
    assert "only be unlocked from the screen" in out[0][0] and calls == []
    out, calls = _voice(tmp_path / "x", "turn on the porch light", can_control=False)
    assert "PIN" in out[0][0] and calls == []
    out, _ = _voice(tmp_path / "y", "turn on the garage light")
    assert "couldn't find" in out[0][0]


def test_voice_patterns_match_commands_not_questions():
    import re
    hit = lambda q: any(re.search(p, q) for p in VOICE_PATTERNS)  # noqa: E731
    for q in ("turn on the living room lights", "turn the kettle off", "set bedroom light to 30 percent",
              "activate movie time", "what devices are offline"):
        assert hit(q), q
    for q in ("what's the weather", "will it rain", "what groceries do i need", "add milk"):
        assert not hit(q), q
