"""Smart home: AURA's device registry, capabilities, scenes and the Home Assistant link.

Home Assistant (a separate local service, see deploy/homeassistant/) talks to the
actual devices — Wi-Fi brands, Matter, Zigbee and so on. AURA keeps only its own choices:

* **Registry** (``smart_device`` in ``data/aura.db``): which Home Assistant entities the
  household added to AURA, with AURA's name, room and favourite flag.
* **Scenes** (``smart_scene``): named lists of explicit device actions.

Live state comes from Home Assistant over its local WebSocket API (``HomeAssistantLink``),
and nothing is invented: with no connection every device is "unknown". Commands are
validated against the device's real capabilities. A success is reported only when Home
Assistant shows the new state; if it only accepted the call, the result says so.
The access token stays on the server (``AURA_HOMEASSISTANT_TOKEN`` in ``.env``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("aura.smarthome")

# --------------------------------------------------------------------------- capabilities

# Home Assistant feature flags (homeassistant/components/<domain>/const.py, checked against 2026.10).
FAN_SET_SPEED = 1
COVER_OPEN, COVER_CLOSE, COVER_SET_POSITION, COVER_STOP = 1, 2, 4, 8
CLIMATE_TARGET_TEMPERATURE, CLIMATE_TURN_OFF, CLIMATE_TURN_ON = 1, 128, 256
MEDIA_PAUSE, MEDIA_VOLUME_SET, MEDIA_TURN_ON, MEDIA_TURN_OFF, MEDIA_PLAY = 1, 4, 128, 256, 16384
FAN_TURN_OFF, FAN_TURN_ON = 16, 32
LIGHT_DIMMABLE_MODES = {"brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"}
LIGHT_COLOR_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}

Kind = Literal["light", "switch", "plug", "fan", "climate", "cover", "sensor", "motion", "door", "smoke", "leak",
               "presence", "lock", "media", "camera", "alarm", "energy", "other"]

# Domains AURA can show. Anything else stays in Home Assistant.
SUPPORTED_DOMAINS = ("light", "switch", "input_boolean", "fan", "climate", "cover", "sensor", "binary_sensor",
                     "lock", "media_player", "camera", "alarm_control_panel")

SENSOR_CLASSES = {"temperature", "humidity", "power", "energy", "illuminance", "pm25", "carbon_dioxide", "battery"}
BINARY_KINDS = {"motion": "motion", "occupancy": "presence", "presence": "presence", "door": "door",
                "window": "door", "opening": "door", "garage_door": "door", "smoke": "smoke", "gas": "smoke",
                "moisture": "leak", "safety": "smoke"}


def describe(entity_id: str, attrs: dict) -> tuple[Kind, list[str]] | None:
    """Kind and capabilities of a Home Assistant entity, from what it reports supporting.
    None when AURA doesn't handle it (e.g. a diagnostic sensor)."""
    domain = entity_id.split(".", 1)[0]
    feats = int(attrs.get("supported_features") or 0)
    if domain == "light":
        modes = set(attrs.get("supported_color_modes") or [])
        caps = ["on_off"]
        if modes & LIGHT_DIMMABLE_MODES:
            caps.append("brightness")
        if "color_temp" in modes:
            caps.append("color_temp")
        if modes & LIGHT_COLOR_MODES:
            caps.append("color")
        return "light", caps
    if domain in ("switch", "input_boolean"):
        return ("plug" if attrs.get("device_class") == "outlet" else "switch"), ["on_off"]
    if domain == "fan":
        caps = ["on_off"] if feats & (FAN_TURN_ON | FAN_TURN_OFF) or not feats else []
        if feats & FAN_SET_SPEED:
            caps.append("fan_speed")
        return "fan", caps or ["on_off"]
    if domain == "climate":
        caps = ["hvac_mode"] if attrs.get("hvac_modes") else []
        if feats & CLIMATE_TARGET_TEMPERATURE:
            caps.append("target_temperature")
        if attrs.get("current_temperature") is not None:
            caps.append("reading")
        return "climate", caps
    if domain == "cover":
        caps = []
        if feats & (COVER_OPEN | COVER_CLOSE):
            caps.append("open_close")
        if feats & COVER_SET_POSITION:
            caps.append("position")
        if feats & COVER_STOP:
            caps.append("stop")
        return "cover", caps
    if domain == "sensor":
        dc = attrs.get("device_class")
        if dc not in SENSOR_CLASSES:
            return None
        return ("energy" if dc in ("power", "energy") else "sensor"), ["reading"]
    if domain == "binary_sensor":
        kind = BINARY_KINDS.get(attrs.get("device_class") or "")
        return (kind, ["reading"]) if kind else None
    if domain == "lock":
        return "lock", ["lock"]
    if domain == "media_player":
        caps = []
        if feats & (MEDIA_TURN_ON | MEDIA_TURN_OFF):
            caps.append("on_off")
        if feats & (MEDIA_PLAY | MEDIA_PAUSE):
            caps.append("play_pause")
        if feats & MEDIA_VOLUME_SET:
            caps.append("volume")
        return "media", caps
    if domain == "camera":
        return "camera", []                      # status only; no video in AURA
    if domain == "alarm_control_panel":
        return "alarm", []                       # status only; arm/disarm stays in Home Assistant
    return None


# Which capability each action needs, and whether it is sensitive (needs confirmation
# and the Pi's own screen or a PIN-unlocked phone).
ACTIONS: dict[str, tuple[str, bool]] = {
    "turn_on": ("on_off", False), "turn_off": ("on_off", False),
    "set_brightness": ("brightness", False), "set_color_temp": ("color_temp", False),
    "set_fan_speed": ("fan_speed", False),
    "set_hvac_mode": ("hvac_mode", False), "set_temperature": ("target_temperature", False),
    "open": ("open_close", False), "close": ("open_close", False), "stop": ("stop", False),
    "set_position": ("position", False),
    "play": ("play_pause", False), "pause": ("play_pause", False), "set_volume": ("volume", False),
    "lock": ("lock", False), "unlock": ("lock", True),
}
SENSITIVE = {a for a, (_, s) in ACTIONS.items() if s}


class CommandError(Exception):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


class Command(BaseModel):
    action: str
    value: float | str | None = None
    # Sent by the screen with every press; the same id twice is answered from memory, not re-run.
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    confirm: bool = False

    @field_validator("action")
    @classmethod
    def _action(cls, v: str) -> str:
        if v not in ACTIONS:
            raise ValueError("unknown action")
        return v


def service_call(entity_id: str, attrs: dict, action: str, value: Any) -> tuple[str, str, dict, Callable[[dict], bool]]:
    """(domain, service, data, done) for a validated action. ``done(state)`` says whether a
    new Home Assistant state shows the action took effect."""
    domain = entity_id.split(".", 1)[0]

    def num(lo: float, hi: float, what: str) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise CommandError(f"{what} needs a number.") from None
        if not lo <= v <= hi:
            raise CommandError(f"{what} must be between {lo:g} and {hi:g}.")
        return v

    def attr_is(key: str, want: float, tol: float = 1.0) -> Callable[[dict], bool]:
        return lambda st: st.get("attributes", {}).get(key) is not None and abs(float(st["attributes"][key]) - want) <= tol

    state_is = lambda *want: (lambda st: st.get("state") in want)  # noqa: E731
    if action in ("turn_on", "turn_off"):
        svc_domain = "homeassistant" if domain == "input_boolean" else domain
        target = "on" if action == "turn_on" else "off"
        if domain == "media_player":
            return domain, action, {}, (state_is("off") if target == "off" else (lambda st: st.get("state") not in ("off", "unavailable", "unknown")))
        return svc_domain, action, {}, state_is(target)
    if action == "set_brightness":
        pct = num(1, 100, "Brightness")
        return "light", "turn_on", {"brightness_pct": round(pct)}, lambda st: (
            st.get("state") == "on" and st.get("attributes", {}).get("brightness") is not None
            and abs(st["attributes"]["brightness"] / 255 * 100 - pct) <= 3)
    if action == "set_color_temp":
        lo, hi = int(attrs.get("min_color_temp_kelvin") or 2000), int(attrs.get("max_color_temp_kelvin") or 6500)
        k = num(lo, hi, "Colour temperature")
        return "light", "turn_on", {"color_temp_kelvin": round(k)}, attr_is("color_temp_kelvin", k, 60)
    if action == "set_fan_speed":
        pct = num(0, 100, "Fan speed")
        return "fan", "set_percentage", {"percentage": round(pct)}, attr_is("percentage", pct, 5)
    if action == "set_hvac_mode":
        modes = attrs.get("hvac_modes") or []
        if value not in modes:
            raise CommandError(f"This device supports: {', '.join(modes) or 'no modes'}.")
        return "climate", "set_hvac_mode", {"hvac_mode": value}, state_is(value)
    if action == "set_temperature":
        t = num(float(attrs.get("min_temp") or 7), float(attrs.get("max_temp") or 35), "Temperature")
        return "climate", "set_temperature", {"temperature": t}, attr_is("temperature", t, 0.3)
    if action in ("open", "close"):
        return "cover", f"{action}_cover", {}, state_is("open", "opening") if action == "open" else state_is("closed", "closing")
    if action == "stop":
        return "cover", "stop_cover", {}, lambda st: st.get("state") in ("open", "closed")
    if action == "set_position":
        p = num(0, 100, "Position")
        return "cover", "set_cover_position", {"position": round(p)}, attr_is("current_position", p, 3)
    if action in ("play", "pause"):
        return "media_player", f"media_{action}", {}, state_is("playing" if action == "play" else "paused")
    if action == "set_volume":
        v = num(0, 100, "Volume")
        return "media_player", "volume_set", {"volume_level": round(v / 100, 2)}, attr_is("volume_level", v / 100, 0.03)
    if action in ("lock", "unlock"):
        return "lock", action, {}, state_is("locked" if action == "lock" else "unlocked")
    raise CommandError("Unsupported action.")


# How each kind of device gets into Home Assistant (shown under "Add devices"). AURA never
# claims a device works before Home Assistant has it set up and reporting.
GUIDES = [
    {"title": "Wi-Fi bulbs and plugs with a phone app (Wipro, Syska, Havells, Tuya / Smart Life…)",
     "text": "Set the device up in its own app first. Then in Home Assistant → Settings → Devices & services → Add integration, "
             "choose the brand (or “Tuya” for Smart Life-based devices) and sign in. Some of these brands work only through "
             "their cloud, so they need the internet."},
    {"title": "Philips WiZ, Shelly, TP-Link Kasa / Tapo, LIFX",
     "text": "These are usually found on your home network automatically. Home Assistant shows them under "
             "“Discovered”, and “Look for devices” lists them here too. Finish the setup in Home Assistant."},
    {"title": "Matter devices (Matter logo)",
     "text": "Needs the Matter Server add-on for Home Assistant (not installed yet), and, for Thread devices, a Thread "
             "border router such as a recent Apple TV, HomePod mini or Google Nest hub. Ask before it is installed."},
    {"title": "Zigbee devices (Aqara, IKEA, Hue bulbs without a bridge)",
     "text": "Needs a Zigbee USB coordinator plugged into the Pi (for example Sonoff ZBDongle-E or Home Assistant Connect ZBT-1), "
             "then the ZHA integration in Home Assistant."},
    {"title": "Philips Hue bridge",
     "text": "Home Assistant finds the bridge on the network; press the bridge's button when asked."},
    {"title": "Smart TVs and speakers",
     "text": "Samsung, LG, Android/Google TV and Chromecast are usually discovered on the network when switched on. "
             "Finish the setup in Home Assistant, often by accepting a prompt on the TV."},
]


# --------------------------------------------------------------------------- AURA's registry + scenes

class DeviceIn(BaseModel):
    entity_id: str = Field(pattern=r"^[a-z_]+\.[a-z0-9_]+$", max_length=120)
    name: str | None = Field(default=None, max_length=40)
    room: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,23}$")
    favorite: bool = False


class DevicePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    room: str | None = Field(default=None, pattern=r"^([a-z0-9][a-z0-9-]{0,23})?$")    # "" = unassigned
    favorite: bool | None = None


class SceneAction(BaseModel):
    entity_id: str = Field(pattern=r"^[a-z_]+\.[a-z0-9_]+$", max_length=120)
    action: str
    value: float | str | None = None

    @field_validator("action")
    @classmethod
    def _action(cls, v: str) -> str:
        if v not in ACTIONS:
            raise ValueError("unknown action")
        if v in SENSITIVE:
            raise ValueError("unlocking can't be part of a scene")
        return v


class SceneRun(BaseModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")


class AutomationToggle(BaseModel):
    enabled: bool


class SceneIn(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    actions: list[SceneAction] = Field(min_length=1, max_length=30)


class Registry:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS smart_device (
                entity_id TEXT PRIMARY KEY, name TEXT, room TEXT, favorite INTEGER NOT NULL DEFAULT 0,
                added_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS smart_scene (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                actions TEXT NOT NULL, updated_at TEXT NOT NULL);
        """)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    # devices
    def devices(self) -> list[dict]:
        rows = self._db.execute("SELECT entity_id, name, room, favorite, added_at FROM smart_device ORDER BY added_at").fetchall()
        return [{"entity_id": e, "name": n, "room": r, "favorite": bool(f), "added_at": a} for e, n, r, f, a in rows]

    def get(self, entity_id: str) -> dict | None:
        return next((d for d in self.devices() if d["entity_id"] == entity_id), None)

    def add(self, d: DeviceIn) -> bool:
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO smart_device VALUES (?,?,?,?,?)",
                                   (d.entity_id, d.name, d.room, int(d.favorite), self._now()))
            return cur.rowcount == 1

    def update(self, entity_id: str, p: DevicePatch) -> bool:
        sets, args = [], []
        for f in ("name", "room", "favorite"):
            if f in p.model_fields_set:
                v = getattr(p, f)
                sets.append(f"{f} = ?")
                args.append(int(v) if f == "favorite" else (v or None))
        if not sets:
            return self.get(entity_id) is not None
        with self._lock:
            return self._db.execute(f"UPDATE smart_device SET {', '.join(sets)} WHERE entity_id = ?",
                                    (*args, entity_id)).rowcount == 1

    def remove(self, entity_id: str) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM smart_device WHERE entity_id = ?", (entity_id,)).rowcount == 1

    # scenes
    def scenes(self) -> list[dict]:
        rows = self._db.execute("SELECT id, name, actions, updated_at FROM smart_scene ORDER BY name").fetchall()
        return [{"id": i, "name": n, "actions": json.loads(a), "updated_at": u} for i, n, a, u in rows]

    def scene(self, scene_id: int) -> dict | None:
        return next((s for s in self.scenes() if s["id"] == scene_id), None)

    def save_scene(self, s: SceneIn, scene_id: int | None = None) -> int:
        data = json.dumps([a.model_dump() for a in s.actions])
        with self._lock:
            try:
                if scene_id is None:
                    return self._db.execute("INSERT INTO smart_scene (name, actions, updated_at) VALUES (?,?,?)",
                                            (s.name.strip(), data, self._now())).lastrowid
                if self._db.execute("UPDATE smart_scene SET name=?, actions=?, updated_at=? WHERE id=?",
                                    (s.name.strip(), data, self._now(), scene_id)).rowcount != 1:
                    raise CommandError("That scene no longer exists.", 404)
                return scene_id
            except sqlite3.IntegrityError:
                raise CommandError("There's already a scene with that name.", 409) from None

    def delete_scene(self, scene_id: int) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM smart_scene WHERE id = ?", (scene_id,)).rowcount == 1


# --------------------------------------------------------------------------- Home Assistant link

Status = Literal["not_configured", "connecting", "connected", "auth_failed", "unreachable"]


class HomeAssistantLink:
    """One WebSocket connection to Home Assistant: a live copy of entity states (from
    ``state_changed`` events), service calls, and the registries for names and models.
    Reconnects by itself with a back-off; one subscription per connection."""

    def __init__(self, url: str, token: str | None, on_change: Callable[[set[str] | None], None] = lambda _: None,
                 connect=None) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.on_change = on_change
        self.status: Status = "not_configured" if not token else "connecting"
        self.message: str | None = None
        self.version: str | None = None
        self.states: dict[str, dict] = {}
        self.entities: dict[str, dict] = {}       # entity registry: platform, device_id, area_id
        self.devices: dict[str, dict] = {}        # device registry: manufacturer, model, name
        self.connected_at: str | None = None
        self._connect = connect                   # tests inject a fake connection
        self._ws = None
        self._ids = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._waiters: list[tuple[str, Callable[[dict], bool], asyncio.Future]] = []
        self._task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()

    @property
    def ws_url(self) -> str:
        return re.sub(r"^http", "ws", self.url) + "/api/websocket"

    def start(self) -> None:
        if self.token and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run(), name="aura-homeassistant")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def _open(self):
        if self._connect:
            return await self._connect(self.ws_url)
        from websockets.asyncio.client import connect
        return await connect(self.ws_url, max_size=32 * 1024 * 1024, open_timeout=10, ping_interval=30)

    async def _run(self) -> None:
        delay = 5
        while True:
            try:
                self.status = "connecting"
                ws = await self._open()
                try:
                    await self._session(ws)
                finally:
                    self._ws = None
                    for f in self._pending.values():
                        if not f.done():
                            f.set_exception(ConnectionError("Home Assistant disconnected"))
                    self._pending.clear()
                    await ws.close()
                delay = 5
            except asyncio.CancelledError:
                raise
            except PermissionError as exc:
                self.status, self.message = "auth_failed", str(exc)
                log.warning("Home Assistant: %s", exc)
                self.on_change(None)
                await asyncio.sleep(300)                    # a wrong token won't fix itself quickly
                continue
            except Exception as exc:  # noqa: BLE001 - any network failure: retry
                if self.status != "unreachable":
                    log.warning("Home Assistant unreachable at %s: %s", self.url, type(exc).__name__)
                self.status, self.message = "unreachable", f"Can't reach Home Assistant at {self.url}."
            self.on_change(None)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _session(self, ws) -> None:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise ConnectionError("unexpected greeting")
        await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        auth = json.loads(await ws.recv())
        if auth.get("type") != "auth_ok":
            raise PermissionError("Home Assistant refused the access token (create a new long-lived token).")
        self.version = auth.get("ha_version")
        self._ws = ws
        reader = asyncio.create_task(self._reader(ws))
        try:
            await self.call("subscribe_events", event_type="state_changed")
            states = await self.call("get_states")
            self.states = {s["entity_id"]: s for s in states}
            await self._load_registries()
            self.status, self.message = "connected", None
            self.connected_at = datetime.now(UTC).isoformat(timespec="seconds")
            log.info("Home Assistant %s connected (%d entities)", self.version, len(self.states))
            self.on_change(None)
            await reader
        finally:
            reader.cancel()

    async def refresh_registries(self) -> None:
        """Re-read names, manufacturers and models (e.g. before showing devices to add)."""
        await self._load_registries()

    async def _load_registries(self) -> None:
        try:
            ents = await self.call("config/entity_registry/list")
            devs = await self.call("config/device_registry/list")
        except Exception:  # noqa: BLE001 - names/models are extras
            return
        self.entities = {e["entity_id"]: e for e in ents}
        self.devices = {d["id"]: d for d in devs}

    async def _reader(self, ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "event":
                data = msg.get("event", {}).get("data", {})
                eid, new = data.get("entity_id"), data.get("new_state")
                if not eid:
                    continue
                if new is None:
                    self.states.pop(eid, None)
                else:
                    self.states[eid] = new
                    for w in list(self._waiters):
                        if w[0] == eid and not w[2].done() and w[1](new):
                            w[2].set_result(True)
                self.on_change({eid})
            elif msg.get("type") == "result":
                f = self._pending.pop(msg.get("id"), None)
                if f and not f.done():
                    if msg.get("success"):
                        f.set_result(msg.get("result"))
                    else:
                        err = msg.get("error") or {}
                        f.set_exception(CommandError(err.get("message") or "Home Assistant refused the request.", 502))
        raise ConnectionError("Home Assistant closed the connection")

    async def call(self, type_: str, timeout: float = 15, **payload) -> Any:
        if self._ws is None:
            raise CommandError("Home Assistant isn't connected.", 503)
        async with self._send_lock:
            self._ids += 1
            mid = self._ids
            fut = asyncio.get_running_loop().create_future()
            self._pending[mid] = fut
            await self._ws.send(json.dumps({"id": mid, "type": type_, **payload}))
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            self._pending.pop(mid, None)
            raise CommandError("Home Assistant didn't answer in time.", 504) from None

    async def call_and_confirm(self, entity_id: str, domain: str, service: str, data: dict,
                               done: Callable[[dict], bool], wait: float = 6) -> Literal["confirmed", "accepted"]:
        """Call a service, then wait for the entity's state to show it took effect."""
        fut = asyncio.get_running_loop().create_future()
        waiter = (entity_id, done, fut)
        self._waiters.append(waiter)
        try:
            await self.call("call_service", domain=domain, service=service, service_data=data,
                            target={"entity_id": entity_id})
            cur = self.states.get(entity_id)
            if cur and done(cur):
                return "confirmed"
            try:
                await asyncio.wait_for(fut, wait)
                return "confirmed"
            except TimeoutError:
                return "accepted"
        finally:
            self._waiters.remove(waiter)

    # ---- facts for the UI ----
    def info(self, entity_id: str) -> dict:
        ent = self.entities.get(entity_id) or {}
        dev = self.devices.get(ent.get("device_id") or "") or {}
        return {"platform": ent.get("platform"), "manufacturer": dev.get("manufacturer"), "model": dev.get("model"),
                "device_name": dev.get("name_by_user") or dev.get("name"), "hidden": bool(ent.get("hidden_by")),
                "disabled": bool(ent.get("disabled_by")), "entity_category": ent.get("entity_category")}


# --------------------------------------------------------------------------- the service AURA uses

def availability(st: dict | None, link_status: str) -> Literal["online", "offline", "unknown"]:
    if link_status != "connected" or st is None:
        return "unknown"
    if st.get("state") == "unavailable":
        return "offline"
    if st.get("state") == "unknown":
        return "unknown"
    return "online"


READING_ATTRS = ("current_temperature", "temperature", "current_humidity", "hvac_modes", "min_temp", "max_temp",
                 "brightness", "color_temp_kelvin", "min_color_temp_kelvin", "max_color_temp_kelvin", "percentage",
                 "current_position", "volume_level", "media_title", "unit_of_measurement", "device_class", "hvac_action")


class SmartHome:
    def __init__(self, registry: Registry, link: HomeAssistantLink, rooms: Callable[[], list[dict]],
                 ha_public_url: Callable[[], str | None]) -> None:
        self.registry = registry
        self.link = link
        self.rooms = rooms
        self.ha_public_url = ha_public_url
        self._recent: dict[str, tuple[float, dict]] = {}     # request_id -> (time, result)
        self._busy: set[str] = set()

    # ---- views ----
    def device_view(self, d: dict) -> dict:
        st = self.link.states.get(d["entity_id"])
        attrs = (st or {}).get("attributes", {})
        desc = describe(d["entity_id"], attrs) if st else None
        kind, caps = desc if desc else ("other", [])
        rooms = {r["id"]: r["name"] for r in self.rooms()}
        room = d["room"] if d["room"] in rooms else None
        return {
            "id": d["entity_id"], "entity_id": d["entity_id"],
            "name": d["name"] or attrs.get("friendly_name") or d["entity_id"],
            "kind": kind, "capabilities": caps, "room": room, "room_name": rooms.get(room) if room else None,
            "favorite": d["favorite"], "availability": availability(st, self.link.status),
            "state": (st or {}).get("state"), "attributes": {k: attrs[k] for k in READING_ATTRS if k in attrs},
            "last_updated": (st or {}).get("last_updated"), "added_at": d["added_at"],
            "integration": "Home Assistant", **({"details": self.link.info(d["entity_id"])} if st else {}),
            "sensitive_actions": sorted(a for a in SENSITIVE if ACTIONS[a][0] in caps),
        }

    def devices(self) -> list[dict]:
        return [self.device_view(d) for d in self.registry.devices()]

    def connection(self) -> dict:
        lk = self.link
        hints = {
            "not_configured": "Home Assistant isn't connected to AURA yet.",
            "connecting": "Connecting to Home Assistant…",
            "auth_failed": "Home Assistant refused AURA's access token. Create a new one and run the connect script again.",
            "unreachable": f"Home Assistant isn't answering at {lk.url}. Is the homeassistant service running?",
            "connected": None,
        }
        return {"status": lk.status, "message": lk.message if lk.status == "auth_failed" else hints[lk.status],
                "version": lk.version, "connected_at": lk.connected_at, "ha_url": self.ha_public_url()}

    def summary(self) -> dict:
        devs = self.devices()
        count = lambda a: sum(1 for d in devs if d["availability"] == a)  # noqa: E731
        rooms = []
        for r in self.rooms():
            mine = [d for d in devs if d["room"] == r["id"]]
            if mine:
                rooms.append({"id": r["id"], "name": r["name"], "total": len(mine),
                              "on": sum(1 for d in mine if d["state"] in ("on", "open", "playing", "unlocked")),
                              "offline": sum(1 for d in mine if d["availability"] == "offline")})
        warnings = [f"{d['name']} is offline" for d in devs if d["availability"] == "offline"]
        alerts = [f"{d['name']}: {'water detected' if d['kind'] == 'leak' else 'smoke detected'}" for d in devs
                  if d["kind"] in ("leak", "smoke") and d["state"] == "on"]
        autos = self.automations()
        return {
            "connection": self.connection(),
            "counts": {"total": len(devs), "online": count("online"), "offline": count("offline"), "unknown": count("unknown")},
            "favorites": [d for d in devs if d["favorite"]][:8],
            "rooms": rooms,
            "unassigned": sum(1 for d in devs if d["room"] is None),
            "scenes": [{"id": s["id"], "name": s["name"], "devices": len({a["entity_id"] for a in s["actions"]})}
                       for s in self.registry.scenes()],
            "automations": {"total": len(autos), "on": sum(1 for a in autos if a["enabled"])},
            "alerts": alerts, "warnings": warnings,
        }

    def candidates(self) -> list[dict]:
        """Devices Home Assistant has that aren't in AURA yet (and that AURA can show)."""
        added = {d["entity_id"] for d in self.registry.devices()}
        out = []
        for eid, st in self.link.states.items():
            if eid in added or eid.split(".", 1)[0] not in SUPPORTED_DOMAINS:
                continue
            desc = describe(eid, st.get("attributes", {}))
            info = self.link.info(eid)
            if desc is None or info["hidden"] or info["disabled"] or info["entity_category"]:
                continue
            out.append({"entity_id": eid, "name": st.get("attributes", {}).get("friendly_name") or eid,
                        "kind": desc[0], "capabilities": desc[1], "availability": availability(st, self.link.status),
                        "manufacturer": info["manufacturer"], "model": info["model"],
                        "connection": info["platform"], "state": st.get("state")})
        return sorted(out, key=lambda c: (c["kind"], c["name"].casefold()))

    async def discovered(self) -> list[dict]:
        """Devices Home Assistant found on the network that still need setting up there."""
        try:
            flows = await self.link.call("config_entries/flow/progress")
        except CommandError:
            return []
        out = []
        for f in flows or []:
            ctx = f.get("context") or {}
            if ctx.get("source") not in ("zeroconf", "ssdp", "dhcp", "homekit", "bluetooth", "usb", "discovery", "mqtt", "matter"):
                continue
            ph = ctx.get("title_placeholders") or {}
            out.append({"flow_id": f.get("flow_id"), "integration": f.get("handler"),
                        "name": ph.get("name") or ph.get("model") or f.get("handler"), "found_by": ctx.get("source")})
        return out

    def automations(self) -> list[dict]:
        if self.link.status != "connected":
            return []
        return sorted(({"entity_id": eid, "name": st.get("attributes", {}).get("friendly_name") or eid,
                        "enabled": st.get("state") == "on", "last_triggered": st.get("attributes", {}).get("last_triggered")}
                       for eid, st in self.link.states.items() if eid.startswith("automation.")),
                      key=lambda a: a["name"].casefold())

    # ---- changes ----
    def add(self, d: DeviceIn) -> dict:
        st = self.link.states.get(d.entity_id)
        if self.link.status != "connected":
            raise CommandError("Home Assistant isn't connected, so the device can't be checked.", 503)
        if st is None:
            raise CommandError("Home Assistant doesn't have that device.", 404)
        if describe(d.entity_id, st.get("attributes", {})) is None or d.entity_id.split(".", 1)[0] not in SUPPORTED_DOMAINS:
            raise CommandError("AURA can't show this kind of device yet.")
        self._check_room(d.room)
        if not self.registry.add(d):
            raise CommandError("That device is already in AURA.", 409)
        return self.device_view(self.registry.get(d.entity_id))

    def update(self, entity_id: str, p: DevicePatch) -> dict:
        if p.room:
            self._check_room(p.room)
        if not self.registry.update(entity_id, p):
            raise CommandError("That device isn't in AURA.", 404)
        return self.device_view(self.registry.get(entity_id))

    def _check_room(self, room: str | None) -> None:
        if room and room not in {r["id"] for r in self.rooms()}:
            raise CommandError("Unknown room. Rooms are managed in Settings → Home & rooms.")

    async def command(self, entity_id: str, cmd: Command) -> dict:
        """Validate, run once, confirm. Results are kept 2 minutes by request id, so a
        repeated or replayed request is answered without touching the device again."""
        now = time.monotonic()
        self._recent = {k: v for k, v in self._recent.items() if now - v[0] < 120}
        if cmd.request_id in self._recent:
            return {**self._recent[cmd.request_id][1], "repeat": True}
        d = self.registry.get(entity_id)
        if d is None:
            raise CommandError("That device isn't in AURA.", 404)
        st = self.link.states.get(entity_id)
        if self.link.status != "connected":
            raise CommandError("Home Assistant isn't connected, so nothing was sent.", 503)
        if st is None or st.get("state") == "unavailable":
            raise CommandError("The device is offline, so nothing was sent.", 409)
        caps = (describe(entity_id, st.get("attributes", {})) or ("other", []))[1]
        need, sensitive = ACTIONS[cmd.action]
        if need not in caps:
            raise CommandError("This device doesn't support that.")
        if sensitive and not cmd.confirm:
            raise CommandError("Unlocking needs confirmation.", 428)
        if entity_id in self._busy:
            raise CommandError("A command for this device is still running.", 409)
        domain, service, data, done = service_call(entity_id, st.get("attributes", {}), cmd.action, cmd.value)
        self._busy.add(entity_id)
        try:
            outcome = await self.link.call_and_confirm(entity_id, domain, service, data, done)
        finally:
            self._busy.discard(entity_id)
        result = {"result": outcome, "device": self.device_view(d),
                  "message": None if outcome == "confirmed" else
                  "Home Assistant accepted the command, but the device hasn't reported the new state yet."}
        self._recent[cmd.request_id] = (now, result)
        log.info("smart home: %s %s -> %s", entity_id, cmd.action, outcome)
        return result

    def scene_check(self, s: SceneIn) -> None:
        added = {d["entity_id"] for d in self.registry.devices()}
        for a in s.actions:
            if a.entity_id not in added:
                raise CommandError(f"{a.entity_id} isn't in AURA.")
            st = self.link.states.get(a.entity_id)
            if st is not None:
                caps = (describe(a.entity_id, st.get("attributes", {})) or ("other", []))[1]
                if ACTIONS[a.action][0] not in caps:
                    raise CommandError(f"{a.entity_id} doesn't support {a.action.replace('_', ' ')}.")

    async def run_scene(self, scene_id: int, request_id: str) -> dict:
        s = self.registry.scene(scene_id)
        if s is None:
            raise CommandError("That scene no longer exists.", 404)
        results = []
        for i, a in enumerate(s["actions"]):                    # only the scene's own actions, in order
            try:
                r = await self.command(a["entity_id"], Command(action=a["action"], value=a.get("value"),
                                                               request_id=f"{request_id[:56]}-{i}"))
                results.append({"entity_id": a["entity_id"], "action": a["action"], "result": r["result"], "message": r["message"]})
            except CommandError as exc:
                results.append({"entity_id": a["entity_id"], "action": a["action"], "result": "failed", "message": str(exc)})
        ok = sum(1 for r in results if r["result"] == "confirmed")
        return {"scene": s["name"], "results": results, "confirmed": ok, "total": len(results)}

    async def set_automation(self, entity_id: str, enabled: bool) -> dict:
        if not entity_id.startswith("automation.") or entity_id not in self.link.states:
            raise CommandError("Unknown automation.", 404)
        want = "on" if enabled else "off"
        outcome = await self.link.call_and_confirm(entity_id, "automation", "turn_on" if enabled else "turn_off", {},
                                                   lambda st: st.get("state") == want)
        return {"result": outcome}


# --------------------------------------------------------------------------- voice / typed commands

VOICE_PATTERNS = (
    r"^(please )?(turn|switch) (on|off)\b", r"^(please )?(turn|switch) .+ (on|off)$",
    r"^(please )?(set|dim) .+ to \d+ ?(percent|%)?$",
    r"^(please )?(activate|run|start) (the )?.+?( scene)?$", r"\bscene\b",
    r"\b(what|which|any) devices? (are |is )?(offline|unavailable)\b", r"\bwhat('s| is) offline\b",
)
_PLURAL_KINDS = {"lights": "light", "light": "light", "lamps": "light", "lamp": "light", "fans": "fan", "fan": "fan",
                 "plugs": "plug", "plug": "plug", "switches": "switch", "curtains": "cover", "curtain": "cover",
                 "blinds": "cover", "blind": "cover", "tv": "media", "ac": "climate", "air conditioner": "climate"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s.lower())).strip()


def resolve(sh: SmartHome, phrase: str) -> tuple[list[dict], str | None]:
    """Devices meant by a spoken phrase like "the living room lights" or "kettle".
    Returns (devices, problem). Several devices only for a room or a plural ("lights")."""
    p = _norm(re.sub(r"\b(the|my|all|please)\b", " ", phrase))
    devs = sh.devices()
    if not p or not devs:
        return [], "There are no smart devices in AURA yet." if not devs else "Which device?"
    exact = [d for d in devs if _norm(d["name"]) == p]
    if exact:
        return exact, None
    room = max((r for r in sh.rooms() if _norm(r["name"]) and _norm(r["name"]) in p), key=lambda r: len(r["name"]), default=None)
    rest = p.replace(_norm(room["name"]), " ").strip() if room else p
    pool = [d for d in devs if d["room"] == room["id"]] if room else devs
    kind = next((k for w, k in _PLURAL_KINDS.items() if re.search(rf"\b{w}\b", rest)), None)
    plural = bool(re.search(r"\b(lights|lamps|fans|plugs|switches|curtains|blinds|everything|all)\b", phrase.lower()))
    named = [d for d in pool if rest and (_norm(d["name"]) in rest or rest in _norm(d["name"]))]
    if named:
        hits = named
    elif kind and not re.sub(r"\b(" + "|".join(map(re.escape, _PLURAL_KINDS)) + r")\b", " ", rest).strip():
        # only a type word is left ("lights"): all of that type here. "garage light" with no garage matches nothing.
        hits = [d for d in pool if d["kind"] == kind or (kind == "plug" and d["kind"] == "switch")]
    elif room and not rest:
        hits = pool
    else:
        hits = []
    if not hits:
        return [], f"I couldn't find a device called “{phrase.strip()}” in AURA."
    if len(hits) > 1 and not (plural or room):
        return [], "Which one: " + ", ".join(d["name"] for d in hits[:6]) + "?"
    return hits, None


async def voice_command(sh: SmartHome, q: str, can_control: bool) -> tuple[str, bool]:
    """Answer one normalized command. Only the actions in ACTIONS, only on devices in AURA,
    never unlocking; the reply says what the devices actually confirmed."""
    if re.search(r"\boffline\b|\bunavailable\b", q):
        if sh.link.status != "connected":
            return "I can't tell: Home Assistant isn't connected.", False
        off = [d["name"] for d in sh.devices() if d["availability"] == "offline"]
        return ("Offline right now: " + ", ".join(off) + ".") if off else "All smart devices are online.", True
    if not can_control:
        return "Changing devices needs the household PIN on this device.", False
    if sh.link.status != "connected":
        return "I can't control devices right now: Home Assistant isn't connected.", False
    if re.search(r"\bunlock\b", q):
        return "For safety, doors can only be unlocked from the screen, with confirmation.", False

    m = re.match(r"^(?:please )?(?:activate|run|start) (?:the )?(.+?)(?: scene)?$", q) or re.search(r"\b(.+?) scene\b", q)
    if m and (re.match(r"^(please )?(activate|run|start)\b", q) or "scene" in q):
        want = _norm(m.group(1))
        scene = next((s for s in sh.registry.scenes() if _norm(s["name"]) == want), None)
        if scene is None:
            names = [s["name"] for s in sh.registry.scenes()]
            return (f"There's no scene called “{m.group(1)}”." + (f" Scenes: {', '.join(names)}." if names else " No scenes are set up yet.")), False
        r = await sh.run_scene(scene["id"], f"voice-{uuid.uuid4().hex}")
        return f"{r['scene']}: {r['confirmed']} of {r['total']} changes confirmed.", r["confirmed"] == r["total"]

    pct = re.match(r"^(?:please )?(?:set|dim) (.+?) to (\d+) ?(?:percent|%)?$", q)
    onoff = re.match(r"^(?:please )?(?:turn|switch) (on|off) (.+)$", q) or re.match(r"^(?:please )?(?:turn|switch) (.+) (on|off)$", q)
    if pct:
        phrase, action, value = pct.group(1), "set_brightness", int(pct.group(2))
    elif onoff:
        a, b = onoff.groups()
        state, phrase = (a, b) if a in ("on", "off") else (b, a)
        action, value = f"turn_{state}", None
    else:
        return "Try “turn on the living room lights” or “activate Movie Time”.", False
    devices, problem = resolve(sh, phrase)
    if problem:
        return problem, False
    done, notes = [], []
    for d in devices:
        if ACTIONS[action][0] not in d["capabilities"]:
            notes.append(f"{d['name']} can't do that")
            continue
        try:
            r = await sh.command(d["entity_id"], Command(action=action, value=value, request_id=f"voice-{uuid.uuid4().hex}"))
            (done if r["result"] == "confirmed" else notes).append(d["name"] if r["result"] == "confirmed" else f"{d['name']} hasn't confirmed yet")
        except CommandError as exc:
            notes.append(f"{d['name']}: {exc}")
    what = {"turn_on": "on", "turn_off": "off"}.get(action, f"at {value}%")
    text = (f"Done: {', '.join(done)} {'is' if len(done) == 1 else 'are'} {what}." if done else "Nothing was changed.")
    if notes:
        text += " " + "; ".join(notes) + "."
    return text, bool(done) and not notes
