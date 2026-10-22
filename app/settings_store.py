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


TileId = Literal["weather", "commute", "advice", "calendar", "groceries", "assistant", "protection", "system"]
# Default order; on the entrance screen it packs into exactly three rows of four columns.
TILES: tuple[str, ...] = ("weather", "commute", "advice", "groceries", "calendar", "assistant", "protection", "system")


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
    return [
        Room(id="entrance", name="Entrance", tiles=["weather", "advice", "commute", "calendar", "assistant"]),
        Room(id="kitchen", name="Kitchen", tiles=["groceries"]),
        Room(id="network", name="Network & system", tiles=["protection", "system"]),
    ]


class HomeSettings(_Strict):
    name: str = "Home"
    tile_order: list[TileId] = Field(default_factory=lambda: list(TILES))
    hidden_tiles: list[TileId] = []
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

    @field_validator("hidden_tiles", mode="before")
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


class AssistantSettings(_Strict):
    enabled: bool = True
    spoken_replies: bool = True              # play answers through the screen's speakers (TV over HDMI)
    max_listen_seconds: int = Field(default=10, ge=3, le=15)


SECTIONS: dict[str, type[_Strict]] = {
    "display": DisplaySettings,
    "weather": WeatherSettings,
    "locations": LocationsSettings,
    "commute": CommuteSettings,
    "groceries": GrocerySettings,
    "assistant": AssistantSettings,
    "home": HomeSettings,
}
Section = Literal["display", "weather", "locations", "commute", "groceries", "assistant", "home"]


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
