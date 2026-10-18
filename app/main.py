"""FastAPI application: JSON API plus the static dashboard and grocery pages."""

from __future__ import annotations

import asyncio
import io
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import segno
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .auth import COOKIE_NAME, AccessControl, LoginLocked
from .config import PROJECT_ROOT, Settings, get_settings
from .commute import CommuteConfig, CommuteMonitor, CommuteStatus, CommuteStore, TomTomProvider
from .events import Broker, TooManySubscribers, stream
from .groceries import (
    CATEGORIES,
    DuplicateItem,
    GroceryList,
    GroceryStore,
    Item,
    ItemIn,
    ItemNotFound,
    ItemPatch,
    ListFull,
    Suggestion,
    VersionConflict,
)
from .location import (
    LocationCooldown,
    LocationError,
    LocationService,
    PublicCityResult,
    PublicLocation,
)
from .network import mdns_url, share_url
from .open_meteo import OpenMeteoProvider
from .recommendations import Recommendation, build_recommendations
from .security import LanOnlyMiddleware
from .weather import (
    MockWeatherProvider,
    WeatherProvider,
    WeatherResponse,
    WeatherService,
    WeatherVisual,
    visual_for,
)

STATIC_DIR = PROJECT_ROOT / "static"
USER_AGENT = f"AURA-home-dashboard/{__version__}"


class WeatherPayload(WeatherResponse):
    recommendations: list[Recommendation] = []
    visual: WeatherVisual | None = None


class LocationChoice(BaseModel):
    id: int


class PinLogin(BaseModel):
    pin: str = Field(min_length=1, max_length=64)


def build_provider(name: str, client: httpx.AsyncClient) -> WeatherProvider | None:
    if name == "open_meteo":
        return OpenMeteoProvider(client)
    if name == "mock":
        return MockWeatherProvider()
    return None


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """``transport`` lets tests replace the network with ``httpx.MockTransport``."""
    settings = settings or get_settings()
    ZoneInfo(settings.default_timezone)  # fail fast on a bad timezone name

    client = httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        transport=transport,
    )
    location = LocationService(
        settings.data_dir / "location.json",
        client,
        detection=settings.location_detection,
        ipinfo_token=settings.ipinfo_token.get_secret_value() if settings.ipinfo_token else None,
        cooldown_seconds=settings.location_detect_cooldown_seconds,
    )
    provider = build_provider(settings.weather_provider, client)
    weather_service = WeatherService(
        provider,
        settings.data_dir / "weather_cache.json",
        refresh_seconds=settings.weather_refresh_seconds,
        retry_seconds=settings.weather_retry_seconds,
        max_stale_hours=settings.weather_max_stale_hours,
    )
    groceries = GroceryStore(settings.data_dir / "aura.db", legacy_json=settings.data_dir / "groceries.json")
    access = AccessControl(
        settings.household_pin.get_secret_value() if settings.household_pin else None,
        settings.data_dir / "session.key",
        session_days=settings.session_days,
        trust_localhost=settings.trust_localhost,
        open_lan=settings.edit_access == "lan",
    )
    broker = Broker()
    background: set[asyncio.Task] = set()

    commute_cfg = CommuteConfig.from_settings(settings)
    commute_key = settings.tomtom_api_key.get_secret_value() if settings.tomtom_api_key else ""
    missing = []
    if settings.commute_provider == "none":
        missing.append("a traffic provider (AURA_COMMUTE_PROVIDER=tomtom)")
    elif not commute_key:
        missing.append("a free TomTom API key (AURA_TOMTOM_API_KEY)")
    if commute_cfg.home is None:
        missing.append("home coordinates (AURA_COMMUTE_HOME)")
    if commute_cfg.office is None:
        missing.append("office coordinates (AURA_COMMUTE_OFFICE)")
    commute_provider = (
        TomTomProvider(client, commute_key)
        if settings.commute_provider == "tomtom" and commute_key else None
    )
    commute_store = CommuteStore(settings.data_dir / "aura.db")
    commute = CommuteMonitor(
        commute_cfg, commute_provider, commute_store,
        lock_path=settings.data_dir / "commute.lock",
        on_change=lambda: broker.publish("commute", {}),
        missing=("Live traffic needs " + ", ".join(missing) + ". See README → Commute.") if missing else None,
    )

    def timezone() -> str:
        loc = location.location
        return (loc.timezone if loc and loc.timezone else None) or settings.default_timezone

    def editor(request: Request) -> None:
        access.require_editor(request)

    def groceries_changed() -> None:
        broker.publish("groceries", {"revision": groceries.revision})

    def location_changed() -> None:
        broker.publish("location", {})

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Detect in the background so the dashboard (and clock) start instantly.
        task = asyncio.create_task(location.ensure_detected())
        background.add(task)
        task.add_done_callback(background.discard)
        commute.start()
        yield
        broker.close()  # let open event streams finish so shutdown isn't blocked
        await commute.stop()
        commute_store.close()
        for t in list(background):
            t.cancel()
        await client.aclose()
        groceries.close()

    app = FastAPI(
        title="AURA", version=__version__, lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.state.location = location
    app.state.weather = weather_service
    app.state.groceries = groceries
    app.state.access = access
    app.state.broker = broker
    app.state.commute = commute
    app.add_middleware(
        LanOnlyMiddleware, allowed_hosts=settings.allowed_hosts, lan_only=settings.lan_only
    )

    @app.middleware("http")
    async def cache_policy(request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        # API data is never cached; static assets revalidate via ETag so an
        # always-on display picks up new versions without a hard refresh.
        is_api = request.url.path.startswith("/api/")
        response.headers["cache-control"] = "no-store" if is_api else "no-cache"
        return response

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "time": datetime.now(ZoneInfo(timezone())).isoformat(timespec="seconds"),
        }

    @app.get("/api/config")
    async def config() -> dict:
        return {
            "timezone": timezone(),
            "clock_format": settings.clock_format,
            "weather_provider": settings.weather_provider,
            "weather_refresh_seconds": settings.weather_refresh_seconds,
            "dev_preview": provider is not None and provider.is_mock,
            "location": location.public().model_dump(mode="json"),
        }

    # ----- session / access -----

    @app.get("/api/session")
    async def get_session(request: Request) -> dict:
        return {
            "can_edit": access.can_edit(request),
            "method": access.method(request),
            "pin_configured": access.pin_configured,
        }

    @app.post("/api/session")
    async def login(body: PinLogin, request: Request, response: Response) -> dict:
        if access.open_lan:
            return {"can_edit": True, "method": "lan", "pin_configured": access.pin_configured}
        if not access.pin_configured:
            raise HTTPException(
                status_code=503,
                detail="No household PIN is set. Add AURA_HOUSEHOLD_PIN to .env on the Pi and restart.",
            )
        client_ip = request.client.host if request.client else "unknown"
        try:
            ok = access.check_pin(client_ip, body.pin.strip())
        except LoginLocked as exc:
            raise HTTPException(
                status_code=429,
                detail=f"Too many wrong attempts. Try again in {max(1, exc.retry_after // 60)} min.",
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
        if not ok:
            raise HTTPException(status_code=401, detail="Wrong PIN")
        response.set_cookie(
            COOKIE_NAME, access.issue(), max_age=access.session_seconds,
            httponly=True, samesite="strict", path="/",
        )
        return {"can_edit": True, "method": "pin", "pin_configured": True}

    @app.delete("/api/session", status_code=204)
    async def logout(response: Response) -> Response:
        response.delete_cookie(COOKIE_NAME, path="/")
        response.status_code = 204
        return response

    # ----- live updates -----

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        try:
            body = stream(broker, request.is_disconnected, {"groceries": groceries.revision})
            # Subscribe now so a client over the limit gets a clean error.
            first = await body.__anext__()
        except TooManySubscribers as exc:
            raise HTTPException(status_code=503, detail="Too many open connections") from exc

        async def gen():
            yield first
            async for chunk in body:
                yield chunk

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # ----- sharing (QR) -----

    @app.get("/api/share")
    async def share() -> dict:
        url, how = share_url(settings.public_url, settings.host, settings.port)
        return {"url": url, "source": how, "mdns_url": mdns_url(settings.port)}

    @app.get("/api/share/qr.svg")
    async def share_qr() -> Response:
        url, how = share_url(settings.public_url, settings.host, settings.port)
        if url is None:
            raise HTTPException(status_code=404, detail=how)
        buf = io.BytesIO()
        segno.make(url, error="m", micro=False).save(
            buf, kind="svg", scale=4, border=2, dark="#0b0e16", light="#ffffff",
            omitsize=True, xmldecl=False, svgns=True,
        )
        return Response(buf.getvalue(), media_type="image/svg+xml")

    # ----- location -----

    @app.get("/api/location", response_model=PublicLocation)
    async def get_location() -> PublicLocation:
        return location.public()

    @app.post("/api/location/detect", response_model=PublicLocation, dependencies=[Depends(editor)])
    async def detect_location() -> PublicLocation:
        try:
            result = await location.detect(force=True)
        except LocationCooldown as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except LocationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        location_changed()
        return result

    @app.post("/api/location/confirm", response_model=PublicLocation, dependencies=[Depends(editor)])
    async def confirm_location() -> PublicLocation:
        try:
            result = location.confirm()
        except LocationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        location_changed()
        return result

    @app.get("/api/location/search", response_model=list[PublicCityResult])
    async def search_location(q: str = Query(min_length=2, max_length=80)) -> list[PublicCityResult]:
        try:
            return await location.search(q)
        except (httpx.HTTPError, LocationError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="City search is unavailable right now") from exc

    @app.put("/api/location", response_model=PublicLocation, dependencies=[Depends(editor)])
    async def set_location(body: LocationChoice) -> PublicLocation:
        try:
            result = location.choose(body.id)
        except LocationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        location_changed()
        return result

    # ----- weather -----

    @app.get("/api/weather", response_model=WeatherPayload)
    async def weather() -> WeatherPayload:
        result = await weather_service.get(location.location, timezone())
        recs: list[Recommendation] = []
        if result.data:
            recs = build_recommendations(
                result.data,
                now=datetime.now(ZoneInfo(result.data.timezone)),
                window_hours=settings.travel_window_hours,
                clock_format=settings.clock_format,
            )
        return WeatherPayload(**result.model_dump(), recommendations=recs, visual=visual_for(result))

    # ----- commute -----

    @app.get("/api/commute", response_model=CommuteStatus)
    async def commute_status() -> CommuteStatus:
        return commute.status()

    # ----- groceries -----

    def conflict(exc: DuplicateItem | VersionConflict) -> JSONResponse:
        code = "duplicate" if isinstance(exc, DuplicateItem) else "conflict"
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc), "code": code, "item": exc.item.model_dump(mode="json")},
        )

    @app.get("/api/groceries", response_model=GroceryList)
    async def list_groceries() -> GroceryList:
        return groceries.load()

    @app.get("/api/groceries/categories")
    async def grocery_categories() -> list[dict]:
        return [{"id": k, "label": v} for k, v in CATEGORIES.items()]

    @app.get("/api/groceries/suggestions", response_model=list[Suggestion])
    async def grocery_suggestions() -> list[Suggestion]:
        return groceries.suggestions()

    @app.post("/api/groceries", status_code=201, response_model=Item, dependencies=[Depends(editor)])
    async def add_grocery(body: ItemIn):
        try:
            item = groceries.add(body)
        except DuplicateItem as exc:
            return conflict(exc)
        except ListFull as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        groceries_changed()
        return item

    @app.patch("/api/groceries/{item_id}", response_model=Item, dependencies=[Depends(editor)])
    async def update_grocery(item_id: int, body: ItemPatch):
        if not body.model_fields_set - {"version"}:
            raise HTTPException(status_code=422, detail="Nothing to change")
        try:
            item = groceries.update(item_id, body)
        except ItemNotFound as exc:
            raise HTTPException(status_code=404, detail="This item was removed on another device") from exc
        except (DuplicateItem, VersionConflict) as exc:
            return conflict(exc)
        groceries_changed()
        return item

    @app.delete("/api/groceries/{item_id}", status_code=204, dependencies=[Depends(editor)])
    async def delete_grocery(item_id: int, version: int | None = Query(default=None, ge=1)):
        try:
            groceries.delete(item_id, version)
        except ItemNotFound as exc:
            raise HTTPException(status_code=404, detail="This item was already removed") from exc
        except VersionConflict as exc:
            return conflict(exc)
        groceries_changed()
        return Response(status_code=204)

    @app.post("/api/groceries/clear-purchased", dependencies=[Depends(editor)])
    async def clear_purchased() -> dict:
        n = groceries.clear_purchased()
        if n:
            groceries_changed()
        return {"deleted": n}

    # ----- pages -----

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/groceries", include_in_schema=False)
    async def groceries_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "groceries.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
