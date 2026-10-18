"""Home location: IP-based detection, manual city search, persistence.

The Pi is stationary, so the home location is detected (or chosen) once and
saved to ``data/location.json``. It never changes on its own: re-detection
only happens on an explicit request from the settings dialog.

IP geolocation is approximate. It locates the internet provider's network
equipment, which can be in another city entirely, so both services are asked
and any disagreement is surfaced for the user to resolve.

Coordinates are stored server-side (rounded to ~1 km) and are never returned
by the public API.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, Field, ValidationError

from .storage import write_json_atomic

log = logging.getLogger(__name__)

COORD_DECIMALS = 2  # ~1.1 km; plenty for weather, avoids storing a precise point

IPINFO_URL = "https://ipinfo.io/json"
IPAPI_URL = "https://ipapi.co/json/"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"


class HomeLocation(BaseModel):
    city: str = Field(min_length=1, max_length=120)
    region: str | None = None
    country: str | None = None
    country_code: str | None = None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str | None = None
    source: Literal["ip", "manual"]
    confirmed: bool = False
    provider: str | None = None
    note: str | None = None
    updated_at: datetime


class LocationFile(BaseModel):
    location: HomeLocation | None = None
    last_detection_at: datetime | None = None
    last_detection_error: str | None = None


class PublicLocation(BaseModel):
    """What the dashboard may see. Deliberately no coordinates."""

    status: Literal["configured", "detecting", "unconfigured"]
    city: str | None = None
    region: str | None = None
    country: str | None = None
    timezone: str | None = None
    source: Literal["ip", "manual"] | None = None
    confirmed: bool = False
    label: str | None = None
    provider: str | None = None
    note: str | None = None
    updated_at: datetime | None = None
    detection_error: str | None = None


class CityResult(BaseModel):
    id: int
    name: str
    region: str | None = None
    district: str | None = None
    country: str | None = None
    country_code: str | None = None
    timezone: str | None = None
    latitude: float
    longitude: float


class PublicCityResult(BaseModel):
    id: int
    name: str
    region: str | None = None
    district: str | None = None
    country: str | None = None


class LocationError(Exception):
    pass


class LocationCooldown(LocationError):
    pass


def _valid_tz(name: str | None) -> str | None:
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def _round(v: float) -> float:
    return round(float(v), COORD_DECIMALS)


# ---------- IP geolocation providers ----------

def parse_ipinfo(data: dict) -> HomeLocation:
    loc = str(data.get("loc") or "")
    city = (data.get("city") or "").strip()
    if not city or "," not in loc:
        raise LocationError("ipinfo.io returned no city")
    lat, lon = (float(x) for x in loc.split(",", 1))
    return HomeLocation(
        city=city,
        region=data.get("region") or None,
        country=data.get("country") or None,
        country_code=data.get("country") or None,
        latitude=_round(lat),
        longitude=_round(lon),
        timezone=_valid_tz(data.get("timezone")),
        source="ip",
        provider="ipinfo.io",
        updated_at=datetime.now(UTC),
    )


def parse_ipapi(data: dict) -> HomeLocation:
    if data.get("error"):
        raise LocationError(f"ipapi.co: {data.get('reason') or 'error'}")
    city = (data.get("city") or "").strip()
    if not city or data.get("latitude") is None or data.get("longitude") is None:
        raise LocationError("ipapi.co returned no city")
    return HomeLocation(
        city=city,
        region=data.get("region") or None,
        country=data.get("country_name") or data.get("country") or None,
        country_code=data.get("country_code") or None,
        latitude=_round(data["latitude"]),
        longitude=_round(data["longitude"]),
        timezone=_valid_tz(data.get("timezone")),
        source="ip",
        provider="ipapi.co",
        updated_at=datetime.now(UTC),
    )


async def _get_json(client: httpx.AsyncClient, url: str, **kw) -> dict:
    res = await client.get(url, **kw)
    res.raise_for_status()
    data = res.json()
    if not isinstance(data, dict):
        raise LocationError(f"Unexpected response from {url}")
    return data


async def detect_ip_location(client: httpx.AsyncClient, ipinfo_token: str | None = None) -> HomeLocation:
    """Ask two IP-geolocation services; prefer ipinfo.io, flag disagreement."""
    headers = {"Authorization": f"Bearer {ipinfo_token}"} if ipinfo_token else {}

    async def ipinfo() -> HomeLocation:
        return parse_ipinfo(await _get_json(client, IPINFO_URL, headers=headers))

    async def ipapi() -> HomeLocation:
        return parse_ipapi(await _get_json(client, IPAPI_URL))

    results = await asyncio.gather(ipinfo(), ipapi(), return_exceptions=True)
    ok = [r for r in results if isinstance(r, HomeLocation)]
    errors = [r for r in results if not isinstance(r, HomeLocation)]
    for err in errors:
        log.warning("IP geolocation provider failed: %s", err)
    if not ok:
        raise LocationError("Location services could not be reached")

    primary = ok[0]
    if len(ok) == 2 and ok[0].city.casefold() != ok[1].city.casefold():
        primary.note = (
            f"Services disagree: {ok[0].provider} says {ok[0].city}, "
            f"{ok[1].provider} says {ok[1].city}. Please confirm or choose your city."
        )
    # Prefer a full country name when the fallback provider supplied one.
    if len(ok) == 2 and primary.country == primary.country_code and ok[1].country_code == primary.country_code:
        primary.country = ok[1].country
    return primary


# ---------- manual city search (Open-Meteo geocoding) ----------

async def search_cities(client: httpx.AsyncClient, query: str, count: int = 8) -> list[CityResult]:
    data = await _get_json(
        client, GEOCODE_URL, params={"name": query, "count": count, "language": "en", "format": "json"}
    )
    out: list[CityResult] = []
    for r in data.get("results") or []:
        try:
            out.append(CityResult(
                id=int(r["id"]),
                name=r["name"],
                region=r.get("admin1"),
                district=r.get("admin2"),
                country=r.get("country"),
                country_code=r.get("country_code"),
                timezone=_valid_tz(r.get("timezone")),
                latitude=float(r["latitude"]),
                longitude=float(r["longitude"]),
            ))
        except (KeyError, TypeError, ValueError, ValidationError):
            continue
    return out


# ---------- service ----------

class LocationService:
    def __init__(
        self,
        path: Path,
        client: httpx.AsyncClient,
        *,
        detection: str = "ip",
        ipinfo_token: str | None = None,
        cooldown_seconds: int = 60,
    ) -> None:
        self.path = path
        self.client = client
        self.detection = detection
        self.ipinfo_token = ipinfo_token
        self.cooldown_seconds = cooldown_seconds
        self.detecting = False
        self._lock = threading.Lock()
        self._detect_lock = asyncio.Lock()
        self._last_attempt = 0.0  # monotonic
        self._search_cache: OrderedDict[str, tuple[float, list[CityResult]]] = OrderedDict()
        self._results_by_id: OrderedDict[int, CityResult] = OrderedDict()
        self.state = self._load()

    # persistence
    def _load(self) -> LocationFile:
        if not self.path.exists():
            return LocationFile()
        try:
            return LocationFile.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            log.error("Could not read %s; treating location as not configured", self.path)
            return LocationFile()

    def _save(self) -> None:
        with self._lock:
            write_json_atomic(self.path, self.state.model_dump(mode="json"))

    # reads
    @property
    def location(self) -> HomeLocation | None:
        return self.state.location

    def public(self) -> PublicLocation:
        loc = self.state.location
        if loc is None:
            return PublicLocation(
                status="detecting" if self.detecting else "unconfigured",
                detection_error=self.state.last_detection_error,
            )
        if loc.source == "manual":
            label = "Set manually"
        elif loc.confirmed:
            label = "Confirmed"
        else:
            label = "Approximate"
        return PublicLocation(
            status="configured",
            city=loc.city,
            region=loc.region,
            country=loc.country,
            timezone=loc.timezone,
            source=loc.source,
            confirmed=loc.confirmed,
            label=label,
            provider=loc.provider,
            note=loc.note if not loc.confirmed else None,
            updated_at=loc.updated_at,
            detection_error=self.state.last_detection_error if self.detecting is False else None,
        )

    # writes
    async def detect(self, *, force: bool = False) -> PublicLocation:
        """Run IP detection. Without ``force`` it is a no-op once a location exists."""
        if self.detection == "off" and not force:
            return self.public()
        if self.state.location is not None and not force:
            return self.public()
        now = time.monotonic()
        if self._last_attempt and now - self._last_attempt < self.cooldown_seconds:
            raise LocationCooldown("Please wait a minute before detecting again")
        async with self._detect_lock:
            self._last_attempt = time.monotonic()
            self.detecting = True
            try:
                loc = await detect_ip_location(self.client, self.ipinfo_token)
            except (LocationError, httpx.HTTPError, ValueError) as exc:
                self.state.last_detection_at = datetime.now(UTC)
                self.state.last_detection_error = (
                    str(exc) if isinstance(exc, LocationError) else "Location services could not be reached"
                )
                self._save()
                raise LocationError(self.state.last_detection_error) from exc
            finally:
                self.detecting = False
            self.state.location = loc
            self.state.last_detection_at = datetime.now(UTC)
            self.state.last_detection_error = None
            self._save()
            log.info("Detected approximate location: %s (%s)", loc.city, loc.provider)
            return self.public()

    async def ensure_detected(self) -> None:
        """Startup hook: detect once if nothing has been saved yet."""
        if self.detection != "ip" or self.state.location is not None:
            return
        try:
            await self.detect()
        except LocationError as exc:
            log.warning("Location detection failed: %s", exc)

    def confirm(self) -> PublicLocation:
        if self.state.location is None:
            raise LocationError("No location to confirm")
        self.state.location.confirmed = True
        self.state.location.note = None
        self.state.location.updated_at = datetime.now(UTC)
        self._save()
        return self.public()

    async def search(self, query: str) -> list[PublicCityResult]:
        key = query.strip().casefold()
        cached = self._search_cache.get(key)
        if cached and time.monotonic() - cached[0] < 86_400:
            results = cached[1]
        else:
            results = await search_cities(self.client, query.strip())
            self._search_cache[key] = (time.monotonic(), results)
            while len(self._search_cache) > 100:
                self._search_cache.popitem(last=False)
        for r in results:
            self._results_by_id[r.id] = r
            self._results_by_id.move_to_end(r.id)
        while len(self._results_by_id) > 500:
            self._results_by_id.popitem(last=False)
        return [PublicCityResult(**r.model_dump(include={"id", "name", "region", "district", "country"}))
                for r in results]

    def choose(self, result_id: int) -> PublicLocation:
        r = self._results_by_id.get(result_id)
        if r is None:
            raise LocationError("That search result has expired. Please search again.")
        self.state.location = HomeLocation(
            city=r.name,
            region=r.region,
            country=r.country,
            country_code=r.country_code,
            latitude=_round(r.latitude),
            longitude=_round(r.longitude),
            timezone=r.timezone,
            source="manual",
            confirmed=True,
            provider="Open-Meteo geocoding",
            updated_at=datetime.now(UTC),
        )
        self._save()
        return self.public()
