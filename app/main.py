"""FastAPI application: JSON API plus the static dashboard and grocery pages."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from datetime import time as dtime
from types import SimpleNamespace
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
from . import sysinfo
from .agenda import AgendaResponse, AgendaService, parse_feeds
from .assistant import Assistant, Sources, Turn
from .dnsfilter import DnsFilterMonitor, DnsFilterStatus
from .commute import CommuteConfig, CommuteMonitor, CommuteStatus, CommuteStore, Point, TomTomProvider
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
from .netinfo import ConnectResult, NetworkService, NetworkStatus, WifiNetwork
from .network import mdns_url, share_url
from .open_meteo import OpenMeteoProvider
from .recommendations import Recommendation, build_recommendations
from .security import LanOnlyMiddleware
from .settings_store import SECTIONS, SettingsError, SettingsStore
from .voice import VoiceClient, VoiceError, VoiceSession
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
    recommendations: list[Recommendation] = []          # top 4, for the dashboard tile
    recommendations_all: list[Recommendation] = []      # every applicable tip, for the detail view
    travel_window_hours: int | None = None
    visual: WeatherVisual | None = None


class LocationChoice(BaseModel):
    id: int


class WifiConnect(BaseModel):
    ssid: str = Field(min_length=1, max_length=32)
    password: str = Field(default="", max_length=64)
    confirm: bool = False
    allow_disconnect: bool = False


class AskIn(BaseModel):
    text: str = Field(min_length=1, max_length=300)


class PinLogin(BaseModel):
    pin: str = Field(min_length=1, max_length=64)


def build_provider(name: str, client: httpx.AsyncClient) -> WeatherProvider | None:
    if name == "open_meteo":
        return OpenMeteoProvider(client)
    if name == "mock":
        return MockWeatherProvider()
    return None


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None,
               voice_transport: httpx.AsyncBaseTransport | None = None,
               dnsfilter: DnsFilterMonitor | None = None) -> FastAPI:
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
    prefs = SettingsStore(settings.data_dir / "aura.db", settings)
    network = NetworkService()
    started_at = sysinfo.now_utc()

    commute_key = settings.tomtom_api_key.get_secret_value() if settings.tomtom_api_key else ""

    def commute_config() -> tuple[CommuteConfig, str | None]:
        """Commute config = .env base + saved Settings (schedule, locations)."""
        c, locs = prefs.commute, prefs.locations
        cfg = dataclasses.replace(
            CommuteConfig.from_settings(settings),
            enabled=c.enabled,
            home=Point(locs.home.latitude, locs.home.longitude) if locs.home else None,
            office=Point(locs.office.latitude, locs.office.longitude) if locs.office else None,
            home_label=locs.home.name if locs.home else "Home",
            office_label=locs.office.name if locs.office else "Office",
            timezone=c.timezone,
            window_start=dtime.fromisoformat(c.window_start),
            window_end=dtime.fromisoformat(c.window_end),
            slot_minutes=c.slot_minutes,
            days=tuple(c.days),
            live_refresh_minutes=c.live_refresh_minutes,
            forecast_refresh_minutes=c.forecast_refresh_minutes,
            max_requests_per_day=c.max_requests_per_day,
            preferred_arrival=dtime.fromisoformat(c.preferred_arrival) if c.preferred_arrival else None,
        )
        missing = []
        if settings.commute_provider == "none":
            missing.append("a traffic provider (AURA_COMMUTE_PROVIDER=tomtom)")
        elif not commute_key:
            missing.append("a free TomTom API key (AURA_TOMTOM_API_KEY)")
        if cfg.home is None:
            missing.append("home coordinates (Settings → Home & office)")
        if cfg.office is None:
            missing.append("office coordinates (Settings → Home & office)")
        note = ("Live traffic needs " + ", ".join(missing) + ". See README → Commute.") if missing else None
        return cfg, note

    commute_cfg, missing_note = commute_config()
    commute_provider = (
        TomTomProvider(client, commute_key)
        if settings.commute_provider == "tomtom" and commute_key else None
    )
    commute_store = CommuteStore(settings.data_dir / "aura.db")
    commute = CommuteMonitor(
        commute_cfg, commute_provider, commute_store,
        lock_path=settings.data_dir / "commute.lock",
        on_change=lambda: broker.publish("commute", {}),
        missing=missing_note,
    )
    weather_service.refresh_seconds = prefs.weather.refresh_minutes * 60

    agenda = AgendaService(
        parse_feeds(settings.calendar_feeds.get_secret_value() if settings.calendar_feeds else ""),
        settings.data_dir / "calendar_cache.json", client,
        timezone=settings.default_timezone,
        refresh_minutes=settings.calendar_refresh_minutes, days_ahead=settings.calendar_days_ahead,
    )
    agenda.on_change = lambda: broker.publish("calendar", {})

    async def current_weather():
        return await weather_service.get(weather_location(), timezone())

    def agenda_for(days: int) -> AgendaResponse:
        agenda.tz_name = timezone()
        return agenda.agenda(days)

    assistant = Assistant(Sources(
        weather=current_weather, commute=lambda: commute.status(), calendar=agenda_for,
        groceries=lambda: groceries.load(), timezone=lambda: timezone(),
        clock_format=lambda: prefs.display.clock_format,
        temperature_unit=lambda: prefs.display.temperature_unit,
        travel_window_hours=lambda: prefs.weather.travel_window_hours,
    ))
    voice = VoiceClient(settings.voice_port, transport=voice_transport)
    voice_session = VoiceSession(voice, assistant, broker.publish)
    dns_filter = dnsfilter or DnsFilterMonitor(
        settings.pihole_api_url,
        settings.pihole_app_password.get_secret_value() if settings.pihole_app_password else None)

    def weather_location():
        """Where weather is fetched for: the city (location dialog), or the exact
        Home coordinates when Settings → Weather says so."""
        loc = location.location
        home = prefs.locations.home
        if prefs.weather.use_home_coordinates and home:
            return SimpleNamespace(
                city=loc.city if loc else home.name, latitude=home.latitude, longitude=home.longitude,
                timezone=(loc.timezone if loc and loc.timezone else None) or prefs.commute.timezone)
        return loc

    async def apply_settings(section: str) -> str:
        """Push saved settings into the running services. Returns a short note."""
        if section in ("commute", "locations"):
            cfg, note = commute_config()
            commute.reconfigure(cfg, missing=note)
            if cfg.enabled and commute.configured:
                commute.start()
            else:
                await commute.stop()
            broker.publish("commute", {})
        if section in ("weather", "locations"):
            weather_service.refresh_seconds = prefs.weather.refresh_minutes * 60
        if section in ("display", "weather", "locations", "groceries", "home"):
            broker.publish("location", {})        # dashboards re-read /api/config and refresh
        if section == "groceries":
            await auto_clear()
        if section == "assistant":
            broker.publish("assistant", {"state": "settings"})
        return {"commute": "The commute monitor uses the new schedule now.",
                "locations": "Commute (and weather, if set) use the new places now.",
                "weather": "Weather uses the new settings from the next update.",
                "display": "Open dashboards update within a few seconds.",
                "groceries": "Phones and the dashboard use the new settings now.",
                "assistant": "The assistant uses the new settings now.",
                "home": "Open dashboards show the new rooms and tiles within a few seconds."}.get(section, "")

    async def auto_clear() -> None:
        days = prefs.groceries.auto_clear_purchased_days
        if days and groceries.clear_purchased_older_than(days):
            groceries_changed()

    async def housekeeping() -> None:
        while True:
            try:
                await auto_clear()
            except Exception:  # never let housekeeping die
                logging.getLogger("aura").exception("Housekeeping failed")
            await asyncio.sleep(3600)

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
        agenda.tz_name = timezone()       # home timezone, once the location is known
        agenda.start()
        hk = asyncio.create_task(housekeeping())
        background.add(hk)
        hk.add_done_callback(background.discard)
        yield
        broker.close()  # let open event streams finish so shutdown isn't blocked
        await commute.stop()
        await agenda.stop()
        commute_store.close()
        for t in list(background):
            t.cancel()
        await client.aclose()
        await voice.aclose()
        await dns_filter.aclose()
        groceries.close()
        prefs.close()

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
    app.state.prefs = prefs
    app.state.agenda = agenda
    app.state.network = network
    app.state.assistant = assistant
    app.state.voice = voice_session
    app.state.dns_filter = dns_filter
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
            "build": started_at.isoformat(),      # changes when the server restarts (dashboards reload)
            "clock_format": prefs.display.clock_format,
            "display": prefs.display.model_dump(),
            "groceries": {"default_filter": prefs.groceries.default_filter},
            "weather_provider": settings.weather_provider,
            "weather_refresh_seconds": prefs.weather.refresh_minutes * 60,
            "dev_preview": provider is not None and provider.is_mock,
            "location": location.public().model_dump(mode="json"),
            "home": prefs.home.model_dump(mode="json"),
            "assistant_enabled": prefs.assistant.enabled,
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
        result = await weather_service.get(weather_location(), timezone())
        recs: list[Recommendation] = []
        if result.data:
            recs = build_recommendations(
                result.data,
                now=datetime.now(ZoneInfo(result.data.timezone)),
                window_hours=prefs.weather.travel_window_hours,
                clock_format=prefs.display.clock_format,
                temperature_unit=prefs.display.temperature_unit,
                limit=50,
            )
        return WeatherPayload(
            **result.model_dump(), recommendations=recs[:4], recommendations_all=recs,
            travel_window_hours=prefs.weather.travel_window_hours, visual=visual_for(result),
        )

    # ----- calendar -----

    @app.get("/api/calendar", response_model=AgendaResponse)
    async def calendar(days: int = Query(default=0, ge=0, le=90)) -> AgendaResponse:
        agenda.tz_name = timezone()
        return agenda.agenda(days or None)

    # ----- commute -----

    @app.get("/api/commute", response_model=CommuteStatus)
    async def commute_status() -> CommuteStatus:
        return commute.status()

    # ----- settings center -----

    def admin(request: Request) -> None:
        access.require_admin(request)

    def settings_info() -> dict:
        cs = commute.status()
        lst = groceries.load()
        loc = location.public().model_dump(mode="json")
        entry = weather_service._entry
        return {
            "weather": {
                "location": loc,
                "using_home_coordinates": bool(prefs.weather.use_home_coordinates and prefs.locations.home),
                "provider": "Open-Meteo" if settings.weather_provider == "open_meteo" else settings.weather_provider,
                "last_update": entry.snapshot.fetched_at.isoformat() if entry else None,
            },
            "commute": {
                "provider": cs.provider, "key_configured": bool(commute_key), "running": cs.monitor_running,
                "state": cs.state, "last_success_at": cs.last_success_at, "requests_today": cs.requests_today,
                "request_cap": cs.request_cap, "message": cs.message,
            },
            "display": {
                "screen_blanking": "Off (screen stays on)",
                "screen_blanking_help": "Change in Raspberry Pi Configuration → Display → Screen Blanking.",
            },
            "groceries": {"items": len(lst.items), "open": lst.remaining, "revision": lst.revision,
                          "connections": broker.count},
            "calendar": [f.model_dump(mode="json") for f in agenda.agenda().feeds],
        }

    async def voice_info() -> dict:
        h = await voice.health()
        return {"available": h is not None, "health": h}

    @app.get("/api/settings", dependencies=[Depends(editor)])
    async def get_settings_center(request: Request) -> dict:
        return {
            "values": {name: prefs.get(name).model_dump(mode="json") for name in SECTIONS},
            "defaults": {name: prefs.defaults(name).model_dump(mode="json") for name in SECTIONS},
            "customized": {name: prefs.customized(name) for name in SECTIONS},
            "can_edit": access.can_edit(request),
            "can_admin": access.can_admin(request),
            "info": {**settings_info(), "assistant": await voice_info()},
        }

    @app.patch("/api/settings/{section}", dependencies=[Depends(editor)])
    async def update_settings(section: str, request: Request):
        try:
            changes = await request.json()
        except ValueError:
            return JSONResponse(status_code=422, content={"detail": {"message": "Body must be JSON.", "errors": []}})
        try:
            new, diff = prefs.update(section, changes)
        except SettingsError as exc:
            status = 404 if exc.errors and exc.errors[0]["message"] == "unknown settings section" else 422
            return JSONResponse(status_code=status,
                                content={"detail": {"message": "Please check the highlighted fields.", "errors": exc.errors}})
        note = await apply_settings(section) if diff else "Nothing changed."
        return {"values": new.model_dump(mode="json"), "changed": sorted(diff), "applied": note}

    @app.post("/api/settings/{section}/reset", dependencies=[Depends(editor)])
    async def reset_settings(section: str):
        if section not in SECTIONS:
            raise HTTPException(status_code=404, detail="Unknown settings section")
        new = prefs.reset(section)
        await apply_settings(section)
        return {"values": new.model_dump(mode="json")}

    @app.get("/api/settings/place/{result_id}", dependencies=[Depends(editor)])
    async def place_from_search(result_id: int) -> dict:
        r = location._results_by_id.get(result_id)
        if r is None:
            raise HTTPException(status_code=404, detail="That search result has expired. Search again.")
        return {"name": r.name, "latitude": round(r.latitude, 6), "longitude": round(r.longitude, 6)}

    @app.get("/api/system/info")
    async def system_info() -> sysinfo.SystemInfo:
        return sysinfo.collect(settings.data_dir, started_at)

    @app.get("/api/system/network", response_model=NetworkStatus)
    async def network_status() -> NetworkStatus:
        return await network.status()

    @app.get("/api/system/dns-filter", response_model=DnsFilterStatus)
    async def dns_filter_status(refresh: bool = False) -> DnsFilterStatus:
        # Read-only; no Pi-hole credentials or controls are ever exposed to browsers.
        return await dns_filter.status(force=refresh)

    @app.post("/api/system/wifi/scan", response_model=list[WifiNetwork], dependencies=[Depends(admin)])
    async def wifi_scan() -> list[WifiNetwork]:
        return await network.scan()

    @app.post("/api/system/wifi/connect", response_model=ConnectResult, dependencies=[Depends(admin)])
    async def wifi_connect(body: WifiConnect) -> ConnectResult:
        if not body.confirm:
            raise HTTPException(status_code=422, detail="Confirm the change first.")
        return await network.connect(body.ssid, body.password, allow_disconnect=body.allow_disconnect)

    # ----- voice assistant -----

    def assistant_on() -> None:
        if not prefs.assistant.enabled:
            raise HTTPException(status_code=403, detail="The assistant is turned off in Settings → Assistant.")

    def mic_allowed(request: Request) -> bool:
        # Only the entrance screen (the Pi's own browser) may switch the microphone on.
        return access.is_local(request)

    def require_mic(request: Request) -> None:
        assistant_on()
        if not mic_allowed(request):
            raise HTTPException(status_code=403,
                                detail="The microphone can only be started from the entrance screen. You can type a question here.")

    def voice_failure(exc: VoiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": str(exc), "retry": exc.retry})

    @app.get("/api/assistant")
    async def assistant_status(request: Request) -> dict:
        h = await voice.health()
        return {
            "enabled": prefs.assistant.enabled,
            "mic_allowed": mic_allowed(request),
            "spoken_replies": prefs.assistant.spoken_replies,
            "max_listen_seconds": prefs.assistant.max_listen_seconds,
            "state": voice_session.state,
            "error": voice_session.error,
            "voice": h,
            "voice_available": h is not None,
            "transcript": [t.model_dump(mode="json") for t in assistant.transcript()],
        }

    @app.post("/api/assistant/listen", response_model=Turn | None)
    async def assistant_listen(request: Request):
        require_mic(request)
        try:
            return await voice_session.listen(prefs.assistant.max_listen_seconds)
        except VoiceError as exc:
            return voice_failure(exc)

    @app.post("/api/assistant/stop", status_code=204)
    async def assistant_stop(request: Request) -> Response:
        require_mic(request)
        try:
            await voice_session.stop()
        except VoiceError as exc:
            return voice_failure(exc)
        return Response(status_code=204)

    @app.post("/api/assistant/cancel", status_code=204)
    async def assistant_cancel(request: Request) -> Response:
        require_mic(request)
        try:
            await voice_session.cancel()
        except VoiceError as exc:
            return voice_failure(exc)
        return Response(status_code=204)

    @app.post("/api/assistant/ask", response_model=Turn)
    async def assistant_ask(body: AskIn) -> Turn:
        assistant_on()
        turn = await assistant.ask(body.text, source="typed")
        broker.publish("assistant", {"state": "answered", "turn_id": turn.id})
        return turn

    @app.delete("/api/assistant/transcript", status_code=204, dependencies=[Depends(editor)])
    async def assistant_clear() -> Response:
        assistant.clear()
        broker.publish("assistant", {"state": "cleared"})
        return Response(status_code=204)

    @app.get("/api/assistant/speech/{turn_id}")
    async def assistant_speech(turn_id: int) -> Response:
        assistant_on()
        if not prefs.assistant.spoken_replies:
            raise HTTPException(status_code=404, detail="Spoken replies are turned off.")
        turn = assistant.get(turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail="That answer was cleared.")
        try:
            wav = await voice.speak(turn.answer.text)
        except VoiceError as exc:
            return voice_failure(exc)
        return Response(wav, media_type="audio/wav")

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
        return groceries.suggestions(defaults=prefs.groceries.quick_add)

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

    @app.get("/settings", include_in_schema=False)
    async def settings_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "settings.html")

    @app.get("/groceries", include_in_schema=False)
    async def groceries_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "groceries.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
