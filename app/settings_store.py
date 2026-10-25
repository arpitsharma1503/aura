"""User-editable settings (the Settings Center).

* Defaults come from the environment / ``.env`` (``app.config.Settings``),
  so existing configuration - including the home/office coordinates and all
  secrets - keeps working and secrets never live here.
* Only values the user changes are stored, per section, as JSON in the
  existing SQLite database (``data/aura.db``, table ``settings``). "Reset"
  deletes a section's overrides so it falls back to the defaults.
* Every section has an explicit schema; updates are partial (unchanged fields
  are preserved), validated as a whole, saved, and only then applied.
* Changes are logged for auditing (field names and safe values only; never
  secrets, and coordinates are logged only as "changed").
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import Settings
from .groceries import COMMON_ITEMS, NAME_MAX, _clean_name
from .storage import make_private

log = logging.getLogger("aura.settings")

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- sections ----------

class DisplaySettings(_Strict):
    clock_format: Literal["12h", "24h"] = "12h"
    temperature_unit: Literal["celsius", "fahrenheit"] = "celsius"
    weather_animations: bool = True
    # "system" follows the device's reduce-motion setting; "reduce" forces it.
    reduced_motion: Literal["system", "reduce"] = "system"
    # Wallpaper behind the glass: AURA's own light, or the household's picture (uploaded
    # separately, see app/background.py). Dim darkens the picture so text stays readable.
    background: Literal["aura", "image"] = "aura"
    background_dim: int = Field(default=40, ge=0, le=80)
    background_version: str = Field(default="", max_length=40)     # set on upload: refreshes screens


class WeatherSettings(_Strict):
    refresh_minutes: int = Field(default=15, ge=1, le=120)
    travel_window_hours: int = Field(default=6, ge=1, le=24)
    # Use the exact Home coordinates (Locations) for weather instead of the
    # city chosen/detected in the location dialog.
    use_home_coordinates: bool = False


class Place(_Strict):
    name: str = Field(min_length=1, max_length=30)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        if not isinstance(v, str):
            raise ValueError("must be text")
        v = re.sub(r"\s+", " ", v).strip()
        if re.search(r"[\x00-\x1f\x7f]", v):
            raise ValueError("contains invalid characters")
        return v


class LocationsSettings(_Strict):
    home: Place | None = None
    office: Place | None = None


def _hhmm(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError("must be a time like 07:30")
    try:
        return time.fromisoformat(v.strip()).strftime("%H:%M")
    except ValueError as exc:
        raise ValueError("must be a time like 07:30") from exc


class CommuteSettings(_Strict):
    enabled: bool = True
    window_start: str = "07:00"
    window_end: str = "11:00"
    timezone: str = "Asia/Kolkata"
    slot_minutes: Literal[5, 10, 15, 20, 30, 60] = 15
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = ["mon", "tue", "wed", "thu", "fri"]
    preferred_arrival: str | None = None
    live_refresh_minutes: int = Field(default=15, ge=5, le=60)
    forecast_refresh_minutes: int = Field(default=30, ge=10, le=240)
    max_requests_per_day: int = Field(default=200, ge=1, le=650)  # TomTom free plan ≈ 650/day

    @field_validator("window_start", "window_end")
    @classmethod
    def _time(cls, v):
        return _hhmm(v)

    @field_validator("preferred_arrival", mode="before")
    @classmethod
    def _arrival(cls, v):
        if v in (None, ""):
            return None
        return _hhmm(v)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown timezone (use a name like Asia/Kolkata)") from exc
        return v

    @field_validator("days")
    @classmethod
    def _days(cls, v):
        if not v:
            raise ValueError("choose at least one day")
        return [d for d in DAYS if d in set(v)]

    @model_validator(mode="after")
    def _window(self):
        if self.window_start >= self.window_end:
            raise ValueError("window_end: must be after the start time")
        return self


class GrocerySettings(_Strict):
    quick_add: list[str] = Field(default_factory=lambda: [n for n, _ in COMMON_ITEMS[:16]], max_length=30)
    default_filter: Literal["needed", "purchased", "all"] = "needed"
    auto_clear_purchased_days: int = Field(default=0, ge=0, le=90)   # 0 = never

    @field_validator("quick_add", mode="before")
    @classmethod
    def _items(cls, v):
        if not isinstance(v, list):
            raise ValueError("must be a list of item names")
        out, seen = [], set()
        for raw in v:
            name = _clean_name(raw)
            if len(name) > NAME_MAX:
                raise ValueError(f"item names must be at most {NAME_MAX} characters")
            if name.casefold() not in seen:
                seen.add(name.casefold())
                out.append(name)
        return out


TileId = Literal["weather", "commute", "advice", "calendar", "groceries", "assistant", "protection", "system", "meals",
                 "smarthome", "prices"]
# Default order; on the entrance screen it packs into exactly three rows of four columns.
TILES: tuple[str, ...] = ("weather", "commute", "meals", "groceries", "advice", "calendar", "assistant", "smarthome",
                          "protection", "system", "prices")


def _label(raw: Any, field: str, max_len: int) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be text")
    text = re.sub(r"\s+", " ", raw).strip()
    if not text:
        raise ValueError(f"{field} can't be empty")
    if len(text) > max_len:
        raise ValueError(f"{field} must be at most {max_len} characters")
    if any(ord(c) < 32 for c in text):
        raise ValueError(f"{field} contains invalid characters")
    return text


def _unique_tiles(v: Any) -> list[str]:
    if not isinstance(v, list):
        raise ValueError("must be a list of tiles")
    out: list[str] = []
    for t in v:
        if t not in TILES:
            raise ValueError(f"unknown tile: {t!r}")
        if t not in out:
            out.append(t)
    return out


class Room(_Strict):
    """A room or zone on the dashboard: a named group of information tiles.
    Rooms don't imply any connected hardware; they only organise tiles."""
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,23}$")
    name: str
    tiles: list[TileId] = []

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        return _label(v, "room name", 24)

    @field_validator("tiles", mode="before")
    @classmethod
    def _tiles(cls, v):
        return _unique_tiles(v)


def _default_rooms() -> list[Room]:
    """The home's rooms: smart-home devices are assigned to these. (Dashboard tabs are views.)"""
    return [Room(id="living-room", name="Living room"), Room(id="bedroom", name="Bedroom"),
            Room(id="kitchen", name="Kitchen"), Room(id="entrance", name="Entrance")]


class HomeSettings(_Strict):
    name: str = "Home"
    # Before views (see ViewsSettings) the dashboard had one Overview tab plus room tabs.
    # These fields are kept so saved settings stay valid and can be migrated into views.
    tile_order: list[TileId] = Field(default_factory=lambda: list(TILES))
    hidden_tiles: list[TileId] = []                          # hidden in every view
    overview_hidden: list[TileId] = Field(default_factory=lambda: ["protection", "system"])
    rooms: list[Room] = Field(default_factory=_default_rooms, max_length=8)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        return _label(v, "home name", 30)

    @field_validator("tile_order", mode="before")
    @classmethod
    def _order(cls, v):
        order = _unique_tiles(v)
        return order + [t for t in TILES if t not in order]     # tiles added later still appear

    @field_validator("hidden_tiles", "overview_hidden", mode="before")
    @classmethod
    def _hidden(cls, v):
        return _unique_tiles(v)

    @model_validator(mode="after")
    def _rooms_unique(self):
        ids = [r.id for r in self.rooms]
        names = [r.name.casefold() for r in self.rooms]
        if len(set(ids)) != len(ids) or len(set(names)) != len(names):
            raise ValueError("room names must be different from each other")
        if "overview" in ids:
            raise ValueError("'overview' is reserved for the whole-home view")
        return self


class MealSettings(_Strict):
    """Monthly meal plan. Names are only display labels for the plan's Person 1 / Person 2."""
    enabled: bool = True
    person1_name: str = "Person 1"
    person2_name: str = "Person 2"
    default_person: Literal["both", "person1", "person2"] = "both"
    # On the 1st: dry goods for the whole month; on plan days 1/8/15/22: that week's fresh items.
    auto_groceries: bool = True

    @field_validator("person1_name", "person2_name", mode="before")
    @classmethod
    def _names(cls, v):
        return _label(v, "name", 20)


class AssistantSettings(_Strict):
    enabled: bool = True
    spoken_replies: bool = True              # play answers through the screen's speakers (TV over HDMI)
    max_listen_seconds: int = Field(default=10, ge=3, le=15)
    # Off: only the entrance screen (the Pi's own browser) can switch the microphone on.
    # On: any phone/laptop on the home network can (the dashboard is LAN-only anyway).
    mic_from_lan: bool = False
    # "Hey Aura": the voice service listens continuously, on the Pi only, for the wake phrase.
    wake_word: bool = False


# --------------------------------------------------------------------------- views

# The widget registry: every dashboard tile, the sizes it can be shown at and its default.
# s = 1 column, w = 2 columns, l = 2 columns x 2 rows (on the entrance screen).
WIDGETS: dict[str, dict[str, Any]] = {
    "weather": {"name": "Weather", "sizes": ("w", "l"), "size": "w", "about": "Now, 5-day outlook, Sun & Moon"},
    "commute": {"name": "Commute", "sizes": ("w", "l"), "size": "w", "about": "Best time to leave for the office"},
    "meals": {"name": "Today's meals", "sizes": ("w", "l"), "size": "w", "about": "Meal plan day and tonight's prep"},
    "groceries": {"name": "Groceries", "sizes": ("w", "l"), "size": "w", "about": "Shared shopping list"},
    "advice": {"name": "Before you go", "sizes": ("s", "w"), "size": "s", "about": "Tips from the weather"},
    "calendar": {"name": "Calendar", "sizes": ("s", "w", "l"), "size": "s", "about": "Events, birthdays, holidays"},
    "assistant": {"name": "Ask AURA", "sizes": ("s", "w"), "size": "s", "about": "Voice assistant status"},
    "smarthome": {"name": "Smart home", "sizes": ("s", "w", "l"), "size": "s", "about": "Devices, favourites, rooms"},
    "protection": {"name": "Network protection", "sizes": ("s", "w"), "size": "s", "about": "Pi-hole ad blocking"},
    "system": {"name": "System", "sizes": ("s", "w"), "size": "s", "about": "Raspberry Pi health"},
    "prices": {"name": "Price Tracker", "sizes": ("s", "w", "l"), "size": "w", "about": "Gold and silver in India (IBJA)"},
}
Size = Literal["s", "w", "l"]
CELLS = {"s": 1, "w": 2, "l": 4}
VIEW_ID = r"^[a-z0-9][a-z0-9-]{0,23}$"


class ViewTile(_Strict):
    id: TileId
    size: Size | None = None              # None: the widget's default size

    @model_validator(mode="after")
    def _size(self):
        if self.size not in WIDGETS[self.id]["sizes"]:
            self.size = WIDGETS[self.id]["size"]         # sizes a widget can't do fall back to its default
        return self


class View(_Strict):
    """A dashboard view: a named set of widgets with their order, sizes and layout.
    Taking a widget out of a view only hides it there; the feature and its data stay."""
    id: str = Field(pattern=VIEW_ID)
    name: str
    description: str = ""
    columns: Literal["auto", "1", "2", "3", "4"] = "auto"      # "auto": 4 on the entrance screen
    tiles: list[ViewTile] = Field(default_factory=list, max_length=len(TILES))

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        return _label(v, "view name", 24)

    @field_validator("description", mode="before")
    @classmethod
    def _desc(cls, v):
        return "" if v in (None, "") else _label(v, "description", 80)

    @field_validator("tiles", mode="before")
    @classmethod
    def _tiles(cls, v):
        if not isinstance(v, list):
            raise ValueError("must be a list of widgets")
        out, seen = [], set()
        for t in v:
            t = {"id": t} if isinstance(t, str) else t.model_dump() if isinstance(t, ViewTile) else t
            if not isinstance(t, dict) or t.get("id") not in TILES:
                raise ValueError(f"unknown widget: {t!r}"[:80])
            if t["id"] not in seen:                         # each widget once per view
                seen.add(t["id"])
                out.append(t)
        return out


def _view(id_: str, name: str, description: str, tiles: list[tuple[str, str | None]], columns: str = "auto") -> View:
    return View(id=id_, name=name, description=description, columns=columns,
                tiles=[ViewTile(id=t, size=sz) for t, sz in tiles])


PRESET_VIEWS: dict[str, View] = {v.id: v for v in (
    # Home = the dashboard as it was before views: exactly the 12 spaces of the entrance screen.
    _view("home", "Home", "Everything important at a glance",
          [("weather", "w"), ("commute", "w"), ("meals", "w"), ("groceries", "w"), ("advice", "s"),
           ("calendar", "s"), ("assistant", "s"), ("smarthome", "s")]),
    _view("daily", "Daily planning", "Calendar, meals, groceries and the commute",
          [("calendar", "l"), ("meals", "w"), ("groceries", "w"), ("commute", "w"), ("advice", "w")]),
    _view("smart-home", "Smart home", "Devices, rooms, scenes and the assistant",
          [("smarthome", "l"), ("assistant", "w"), ("weather", "w")]),
    _view("system", "System", "Pi health, network protection and services",
          [("system", "w"), ("protection", "w"), ("assistant", "w"), ("smarthome", "w")]),
    _view("prices", "Price Tracker", "Gold & Silver · India", [("prices", "l")], columns="2"),
)}


def _preset_views() -> list[View]:
    return [v.model_copy(deep=True) for v in PRESET_VIEWS.values()]


def views_from_home(home: HomeSettings) -> list[View]:
    """Views for a household that arranged the old Overview/room tabs: the Overview becomes
    "Home" (same tiles, same order) and each room with tiles becomes a view of its own."""
    hidden = set(home.hidden_tiles)
    overview = [t for t in home.tile_order if t not in hidden and t not in home.overview_hidden]
    views = [_view("home", "Home", "Everything important at a glance", [(t, None) for t in overview])]
    for r in home.rooms:
        tiles = [t for t in r.tiles if t not in hidden]
        if tiles and r.id != "home" and len(views) < 8:
            views.append(_view(r.id, r.name, "", [(t, None) for t in tiles]))
    for preset in ("smart-home", "system", "prices"):       # the new widgets get somewhere to live
        if len(views) < 8 and all(v.id != preset and v.name.casefold() != PRESET_VIEWS[preset].name.casefold() for v in views):
            views.append(PRESET_VIEWS[preset].model_copy(deep=True))
    return views


class ViewsSettings(_Strict):
    """Dashboard views (the tabs under the clock). Stored apart from the widgets' own data."""
    version: int = 1
    views: list[View] = Field(default_factory=_preset_views, min_length=1, max_length=8)
    default_view: str = Field(default="home", pattern=VIEW_ID)

    @model_validator(mode="after")
    def _check(self):
        ids = [v.id for v in self.views]
        names = [v.name.casefold() for v in self.views]
        if len(set(ids)) != len(ids) or len(set(names)) != len(names):
            raise ValueError("view names must be different from each other")
        if self.default_view not in ids:
            self.default_view = ids[0]                      # the default view was deleted: use the first
        return self


def _arranged(home_overrides: dict) -> bool:
    return bool(home_overrides.get("tile_order") or home_overrides.get("overview_hidden") is not None
                or any(r.get("tiles") for r in home_overrides.get("rooms") or [] if isinstance(r, dict)))


SECTIONS: dict[str, type[_Strict]] = {
    "display": DisplaySettings,
    "weather": WeatherSettings,
    "locations": LocationsSettings,
    "commute": CommuteSettings,
    "groceries": GrocerySettings,
    "assistant": AssistantSettings,
    "home": HomeSettings,
    "meals": MealSettings,
    "views": ViewsSettings,                       # after "home": first use migrates the old tabs
}
Section = Literal["display", "weather", "locations", "commute", "groceries", "assistant", "home", "meals", "views"]


def env_defaults(env: Settings) -> dict[str, dict[str, Any]]:
    """Defaults for every section, taken from .env / environment."""
    def place(value: str, name: str) -> dict | None:
        if not value:
            return None
        lat, lon = (float(x) for x in value.split(","))
        return {"name": name, "latitude": lat, "longitude": lon}

    return {
        "display": {"clock_format": env.clock_format},
        "weather": {
            "refresh_minutes": max(1, min(120, round(env.weather_refresh_seconds / 60))),
            "travel_window_hours": env.travel_window_hours,
        },
        "locations": {
            "home": place(env.commute_home, env.commute_home_label),
            "office": place(env.commute_office, env.commute_office_label),
        },
        "commute": {
            "enabled": env.commute_enabled,
            "window_start": env.commute_window_start,
            "window_end": env.commute_window_end,
            "timezone": env.commute_timezone,
            "slot_minutes": env.commute_slot_minutes if env.commute_slot_minutes in (5, 10, 15, 20, 30, 60) else 15,
            "days": list(env.commute_days),
            "live_refresh_minutes": env.commute_live_refresh_minutes,
            "forecast_refresh_minutes": env.commute_forecast_refresh_minutes,
            "max_requests_per_day": min(env.commute_max_requests_per_day, 650),
        },
        "groceries": {},
        "assistant": {"enabled": env.voice_enabled},
        "home": {},
        "meals": {},
    }


class SettingsError(Exception):
    def __init__(self, errors: list[dict]) -> None:
        super().__init__("; ".join(f"{e['field']}: {e['message']}" for e in errors))
        self.errors = errors


def _errors(exc: ValidationError, section: str) -> list[dict]:
    out = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e["loc"]) or section
        msg = e["msg"].removeprefix("Value error, ")
        if loc == section and ": " in msg:          # model-level errors carry their field
            loc, msg = msg.split(": ", 1)
        out.append({"field": loc, "message": msg})
    return out


class SettingsStore:
    def __init__(self, db_path: Path, env: Settings) -> None:
        self.env = env
        self._defaults = env_defaults(env)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("""CREATE TABLE IF NOT EXISTS settings (
            section TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        make_private(db_path)
        self._lock = threading.Lock()
        self._cache: dict[str, _Strict] = {}
        for name in SECTIONS:
            self._cache[name] = self._load(name)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ----- reads -----

    def _overrides(self, section: str) -> dict:
        row = self._db.execute("SELECT data FROM settings WHERE section = ?", (section,)).fetchone()
        if not row:
            return {}
        try:
            data = json.loads(row[0])
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _load(self, section: str) -> _Strict:
        model = SECTIONS[section]
        defaults = self._defaults.get(section, {})
        overrides = self._overrides(section)
        if section == "views" and not overrides and _arranged(self._overrides("home")):
            # The household arranged the old Overview/room tabs: keep that as their views, and
            # save it so later changes to rooms can't change the views.
            migrated = ViewsSettings(views=views_from_home(self._cache["home"]))
            self._db.execute("INSERT OR REPLACE INTO settings (section, data, updated_at) VALUES (?, ?, ?)",
                             ("views", json.dumps({"views": migrated.model_dump(mode="json")["views"]}),
                              datetime.now(UTC).isoformat(timespec="seconds")))
            log.info("settings: old dashboard tabs migrated to %d views", len(migrated.views))
            return migrated
        try:
            return model.model_validate({**defaults, **overrides})
        except ValidationError:
            log.error("Stored %s settings are invalid; using defaults", section)
            return model.model_validate(defaults)

    def get(self, section: str) -> _Strict:
        return self._cache[section]

    @property
    def display(self) -> DisplaySettings: return self._cache["display"]  # noqa: E704
    @property
    def weather(self) -> WeatherSettings: return self._cache["weather"]  # noqa: E704
    @property
    def locations(self) -> LocationsSettings: return self._cache["locations"]  # noqa: E704
    @property
    def commute(self) -> CommuteSettings: return self._cache["commute"]  # noqa: E704
    @property
    def groceries(self) -> GrocerySettings: return self._cache["groceries"]  # noqa: E704
    @property
    def assistant(self) -> AssistantSettings: return self._cache["assistant"]  # noqa: E704
    @property
    def home(self) -> HomeSettings: return self._cache["home"]  # noqa: E704
    @property
    def meals(self) -> MealSettings: return self._cache["meals"]  # noqa: E704
    @property
    def views(self) -> ViewsSettings: return self._cache["views"]  # noqa: E704

    def customized(self, section: str) -> list[str]:
        return sorted(self._overrides(section))

    def defaults(self, section: str) -> _Strict:
        return SECTIONS[section].model_validate(self._defaults.get(section, {}))

    # ----- writes -----

    def update(self, section: str, changes: dict) -> tuple[_Strict, dict[str, tuple]]:
        """Merge ``changes`` into a section, validate, persist. Returns (new, diff)."""
        if section not in SECTIONS:
            raise SettingsError([{"field": "section", "message": "unknown settings section"}])
        if not isinstance(changes, dict) or not changes:
            raise SettingsError([{"field": section, "message": "nothing to change"}])
        model = SECTIONS[section]
        unknown = set(changes) - set(model.model_fields)
        if unknown:
            raise SettingsError([{"field": f, "message": "unknown setting"} for f in sorted(unknown)])
        with self._lock:
            current = self._cache[section]
            merged = {**current.model_dump(), **changes}
            try:
                new = model.model_validate(merged)
            except ValidationError as exc:
                raise SettingsError(_errors(exc, section)) from exc
            diff = {k: (getattr(current, k), getattr(new, k)) for k in model.model_fields
                    if getattr(current, k) != getattr(new, k)}
            if diff:
                overrides = {**self._overrides(section), **{k: new.model_dump(mode="json")[k] for k in diff}}
                self._db.execute(
                    "INSERT OR REPLACE INTO settings (section, data, updated_at) VALUES (?, ?, ?)",
                    (section, json.dumps(overrides), datetime.now(UTC).isoformat(timespec="seconds")))
                self._cache[section] = new
                self._audit(section, diff)
            return new, diff

    def reset(self, section: str) -> _Strict:
        if section not in SECTIONS:
            raise SettingsError([{"field": "section", "message": "unknown settings section"}])
        with self._lock:
            self._db.execute("DELETE FROM settings WHERE section = ?", (section,))
            self._cache[section] = self._load(section)
            log.info("settings: %s reset to defaults", section)
            return self._cache[section]

    @staticmethod
    def _audit(section: str, diff: dict) -> None:
        parts = []
        for k, (old, new) in diff.items():
            if section == "locations":
                parts.append(f"{k} changed")                 # never log precise coordinates
            elif isinstance(new, list) and len(str(new)) > 80:
                parts.append(f"{k} changed ({len(new)} entries)")
            else:
                parts.append(f"{k}: {old!r} -> {new!r}")
        log.info("settings: %s updated (%s)", section, "; ".join(parts))
