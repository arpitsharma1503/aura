"""Weekday home -> office commute monitor.

What it does
------------
* On configured weekdays (default Mon-Fri, minus configured holidays) it
  evaluates departure times every ``slot_minutes`` between ``window_start``
  and ``window_end`` (default 07:00-11:00, Asia/Kolkata).
* Shortly before the window (``prefetch_minutes``) it asks the routing
  provider for a forecast for every slot; during the window it refreshes the
  remaining slots and also asks for a *live* "leave now" estimate.
* Every answer is stored in SQLite (``commute_observations``) so weekday
  patterns can be studied later, and so a recent result survives restarts.
* Outside the window nothing is requested.

Honesty rules
-------------
* Durations come only from the provider or from the user's own stored past
  observations. Nothing is invented; with no provider/key the API says so.
* Each figure is labelled with its basis: ``live`` (current traffic),
  ``forecast`` (provider prediction for a future departure, based on
  historical traffic patterns) or ``historical`` (median of this household's
  own past observations for that weekday and time).

Provider
--------
TomTom Routing API (``calculateRoute``) with ``traffic=true``,
``computeTravelTimeFor=all`` and ``departAt``. Free plan: 20,000 routing
requests/month, no credit card. AURA additionally enforces
``max_requests_per_day``.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import sqlite3
import statistics
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel

from .storage import make_private

log = logging.getLogger(__name__)

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TOMTOM_URL = "https://api.tomtom.com/routing/1/calculateRoute/{o}:{d}/json"

Basis = Literal["live", "forecast", "historical"]


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Point:
    lat: float
    lon: float

    @classmethod
    def parse(cls, value: str) -> Point | None:
        if not value:
            return None
        lat, lon = (float(x) for x in value.split(","))
        return cls(lat, lon)

    def __str__(self) -> str:
        return f"{self.lat},{self.lon}"


@dataclass(frozen=True)
class CommuteConfig:
    home: Point | None
    office: Point | None
    home_label: str = "Home"
    office_label: str = "Office"
    timezone: str = "Asia/Kolkata"
    window_start: time = time(7, 0)
    window_end: time = time(11, 0)
    slot_minutes: int = 15
    days: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri")
    holidays: frozenset[date] = field(default_factory=frozenset)
    prefetch_minutes: int = 15
    live_refresh_minutes: int = 15
    forecast_refresh_minutes: int = 30
    max_requests_per_day: int = 200
    history_days: int = 180
    enabled: bool = True

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_settings(cls, s) -> CommuteConfig:
        return cls(
            home=Point.parse(s.commute_home),
            office=Point.parse(s.commute_office),
            home_label=s.commute_home_label,
            office_label=s.commute_office_label,
            timezone=s.commute_timezone,
            window_start=time.fromisoformat(s.commute_window_start),
            window_end=time.fromisoformat(s.commute_window_end),
            slot_minutes=s.commute_slot_minutes,
            days=tuple(s.commute_days),
            holidays=frozenset(s.commute_holidays),
            prefetch_minutes=s.commute_prefetch_minutes,
            live_refresh_minutes=s.commute_live_refresh_minutes,
            forecast_refresh_minutes=s.commute_forecast_refresh_minutes,
            max_requests_per_day=s.commute_max_requests_per_day,
            history_days=s.commute_history_days,
            enabled=s.commute_enabled,
        )


# ---------------------------------------------------------------------------
# schedule (pure functions)
# ---------------------------------------------------------------------------

ScheduleStatus = Literal["prefetch", "active", "before_window", "after_window", "off_day"]


class ScheduleState(BaseModel):
    status: ScheduleStatus
    day_reason: Literal["weekend", "holiday", "not_scheduled"] | None = None
    service_date: date
    window_start: datetime
    window_end: datetime
    next_window_start: datetime


def window_bounds(cfg: CommuteConfig, d: date) -> tuple[datetime, datetime]:
    tz = cfg.tz
    return datetime.combine(d, cfg.window_start, tz), datetime.combine(d, cfg.window_end, tz)


def day_status(cfg: CommuteConfig, d: date) -> str | None:
    """None if monitoring runs on this date, else the reason it doesn't."""
    if d in cfg.holidays:
        return "holiday"
    if DAY_NAMES[d.weekday()] not in cfg.days:
        return "weekend" if d.weekday() >= 5 else "not_scheduled"
    return None


def slots(cfg: CommuteConfig, d: date) -> list[datetime]:
    """Departure options from window start to window end inclusive."""
    start, end = window_bounds(cfg, d)
    out, t = [], start
    while t <= end:
        out.append(t)
        t += timedelta(minutes=cfg.slot_minutes)
    return out


def next_window_start(cfg: CommuteConfig, now: datetime) -> datetime:
    local = now.astimezone(cfg.tz)
    d = local.date()
    for _ in range(366):
        start, _end = window_bounds(cfg, d)
        if day_status(cfg, d) is None and start - timedelta(minutes=cfg.prefetch_minutes) > local:
            return start
        d += timedelta(days=1)
    return window_bounds(cfg, d)[0]


def schedule_state(cfg: CommuteConfig, now: datetime) -> ScheduleState:
    local = now.astimezone(cfg.tz)
    today = local.date()
    start, end = window_bounds(cfg, today)
    reason = day_status(cfg, today)
    nxt = next_window_start(cfg, local)
    if reason is not None:
        status: ScheduleStatus = "off_day"
    elif local < start - timedelta(minutes=cfg.prefetch_minutes):
        status = "before_window"
    elif local < start:
        status = "prefetch"
    elif local <= end:
        status = "active"
    else:
        status = "after_window"
    return ScheduleState(status=status, day_reason=reason, service_date=today,
                         window_start=start, window_end=end, next_window_start=nxt)


# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------

class RouteEstimate(BaseModel):
    departure_at: datetime
    arrival_at: datetime
    duration_s: int
    no_traffic_s: int | None = None
    historic_s: int | None = None
    live_s: int | None = None
    delay_s: int | None = None
    length_m: int | None = None
    basis: Literal["live", "forecast"]
    provider: str


class CommuteProviderError(Exception):
    def __init__(self, kind: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind  # auth | rate_limit | timeout | unavailable | bad_response
        self.retry_after = retry_after


class TomTomProvider:
    name = "TomTom"

    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        self.client = client
        self._key = api_key

    async def route(self, origin: Point, dest: Point, depart_at: datetime | None) -> RouteEstimate:
        params = {
            "key": self._key,
            "traffic": "true",
            "travelMode": "car",
            "routeType": "fastest",
            "computeTravelTimeFor": "all",
            "routeRepresentation": "summaryOnly",
            "departAt": "now" if depart_at is None else depart_at.isoformat(timespec="seconds"),
        }
        url = TOMTOM_URL.format(o=origin, d=dest)
        try:
            res = await self.client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise CommuteProviderError("timeout", "Traffic service timed out") from exc
        except httpx.HTTPError as exc:
            raise CommuteProviderError("unavailable", "Traffic service unreachable") from exc
        if res.status_code in (401, 403):
            raise CommuteProviderError("auth", "TomTom rejected the API key (check AURA_TOMTOM_API_KEY)")
        if res.status_code == 429:
            retry = res.headers.get("retry-after")
            raise CommuteProviderError("rate_limit", "TomTom rate limit reached",
                                       int(retry) if retry and retry.isdigit() else None)
        if res.status_code >= 400:
            raise CommuteProviderError("unavailable", f"Traffic service error (HTTP {res.status_code})")
        try:
            summary = res.json()["routes"][0]["summary"]
            duration = int(summary["travelTimeInSeconds"])
            dep = datetime.fromisoformat(summary["departureTime"]) if summary.get("departureTime") else None
            arr = datetime.fromisoformat(summary["arrivalTime"]) if summary.get("arrivalTime") else None
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise CommuteProviderError("bad_response", "Unexpected response from traffic service") from exc
        if duration <= 0:
            raise CommuteProviderError("bad_response", "Traffic service returned an invalid duration")
        dep = dep or depart_at or datetime.now(UTC)
        arr = arr or dep + timedelta(seconds=duration)

        def opt(k):
            v = summary.get(k)
            return int(v) if isinstance(v, (int, float)) else None

        return RouteEstimate(
            departure_at=dep, arrival_at=arr, duration_s=duration,
            no_traffic_s=opt("noTrafficTravelTimeInSeconds"),
            historic_s=opt("historicTrafficTravelTimeInSeconds"),
            live_s=opt("liveTrafficIncidentsTravelTimeInSeconds"),
            delay_s=opt("trafficDelayInSeconds"),
            length_m=opt("lengthInMeters"),
            basis="live" if depart_at is None else "forecast",
            provider=self.name,
        )


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS commute_observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at   TEXT NOT NULL,
    service_date  TEXT NOT NULL,
    weekday       INTEGER NOT NULL,
    slot          TEXT NOT NULL,          -- 'HH:MM' for planned slots, 'now' for live checks
    departure_at  TEXT NOT NULL,
    arrival_at    TEXT NOT NULL,
    duration_s    INTEGER NOT NULL,
    no_traffic_s  INTEGER,
    historic_s    INTEGER,
    live_s        INTEGER,
    delay_s       INTEGER,
    length_m      INTEGER,
    basis         TEXT NOT NULL,
    provider      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS commute_by_day ON commute_observations (service_date, slot);
CREATE INDEX IF NOT EXISTS commute_by_weekday ON commute_observations (weekday, slot, basis);
CREATE TABLE IF NOT EXISTS commute_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Observation(BaseModel):
    observed_at: datetime
    service_date: date
    slot: str
    departure_at: datetime
    arrival_at: datetime
    duration_s: int
    no_traffic_s: int | None
    delay_s: int | None
    length_m: int | None
    basis: str
    provider: str


class CommuteStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)
        make_private(db_path)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record(self, est: RouteEstimate, slot: str, service_date: date, observed_at: datetime) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO commute_observations (observed_at, service_date, weekday, slot, departure_at,
                   arrival_at, duration_s, no_traffic_s, historic_s, live_s, delay_s, length_m, basis, provider)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (observed_at.isoformat(), service_date.isoformat(), service_date.weekday(), slot,
                 est.departure_at.isoformat(), est.arrival_at.isoformat(), est.duration_s, est.no_traffic_s,
                 est.historic_s, est.live_s, est.delay_s, est.length_m, est.basis, est.provider),
            )

    @staticmethod
    def _obs(r: sqlite3.Row) -> Observation:
        return Observation(
            observed_at=r["observed_at"], service_date=r["service_date"], slot=r["slot"],
            departure_at=r["departure_at"], arrival_at=r["arrival_at"], duration_s=r["duration_s"],
            no_traffic_s=r["no_traffic_s"], delay_s=r["delay_s"], length_m=r["length_m"],
            basis=r["basis"], provider=r["provider"],
        )

    def latest_for_day(self, d: date) -> dict[str, Observation]:
        """Most recent observation per slot (and for 'now') on a date."""
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM commute_observations o WHERE service_date = ? AND observed_at = (
                     SELECT MAX(observed_at) FROM commute_observations
                     WHERE service_date = o.service_date AND slot = o.slot)""",
                (d.isoformat(),),
            ).fetchall()
        return {r["slot"]: self._obs(r) for r in rows}

    def typical(self, weekday: int, slot_time: time, before: date, weeks: int = 8) -> tuple[int, int] | None:
        """(median seconds, samples) of this household's past live checks near a slot."""
        lo = (datetime.combine(date(2000, 1, 1), slot_time) - timedelta(minutes=8)).time()
        hi = (datetime.combine(date(2000, 1, 1), slot_time) + timedelta(minutes=8)).time()
        since = (before - timedelta(weeks=weeks)).isoformat()
        with self._lock:
            rows = self._db.execute(
                """SELECT departure_at, duration_s FROM commute_observations
                   WHERE weekday = ? AND basis = 'live' AND service_date >= ? AND service_date < ?""",
                (weekday, since, before.isoformat()),
            ).fetchall()
        samples = [r["duration_s"] for r in rows if lo <= datetime.fromisoformat(r["departure_at"]).time() <= hi]
        if len(samples) < 3:
            return None
        return int(statistics.median(samples)), len(samples)

    def meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM commute_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self._db.execute("DELETE FROM commute_meta WHERE key = ?", (key,))
            else:
                self._db.execute("INSERT OR REPLACE INTO commute_meta (key, value) VALUES (?, ?)", (key, value))

    def count_request(self, d: date) -> int:
        key = f"requests:{d.isoformat()}"
        with self._lock:
            self._db.execute(
                """INSERT INTO commute_meta (key, value) VALUES (?, '1')
                   ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1""", (key,))
            return int(self._db.execute("SELECT value FROM commute_meta WHERE key = ?", (key,)).fetchone()[0])

    def requests(self, d: date) -> int:
        v = self.meta(f"requests:{d.isoformat()}")
        return int(v) if v else 0

    def prune(self, keep_days: int, today: date) -> int:
        cutoff = (today - timedelta(days=keep_days)).isoformat()
        with self._lock:
            n = self._db.execute("DELETE FROM commute_observations WHERE service_date < ?", (cutoff,)).rowcount
            self._db.execute("DELETE FROM commute_meta WHERE key LIKE 'requests:%' AND key < ?",
                             (f"requests:{cutoff}",))
        return n


# ---------------------------------------------------------------------------
# public status model
# ---------------------------------------------------------------------------

TrafficLevel = Literal["light", "moderate", "heavy", "severe"]


def traffic_level(duration_s: int, no_traffic_s: int | None) -> TrafficLevel | None:
    if not no_traffic_s or no_traffic_s <= 0:
        return None
    extra = (duration_s - no_traffic_s) / no_traffic_s
    if extra < 0.15:
        return "light"
    if extra < 0.35:
        return "moderate"
    if extra < 0.65:
        return "heavy"
    return "severe"


class SlotView(BaseModel):
    departure_at: datetime
    arrival_at: datetime | None = None
    duration_min: int | None = None
    traffic_level: TrafficLevel | None = None
    delay_min: int | None = None
    basis: Basis | None = None
    observed_at: datetime | None = None
    past: bool = False
    best: bool = False
    samples: int | None = None


class Recommendation(BaseModel):
    departure_at: datetime
    arrival_at: datetime
    duration_min: int
    traffic_level: TrafficLevel | None
    delay_min: int | None
    basis: Basis
    basis_label: str
    leave_now: bool = False


class CommuteStatus(BaseModel):
    state: Literal["unconfigured", "disabled", "active", "prefetch", "before_window",
                   "after_window", "off_day"]
    message: str
    configured: bool
    provider: str | None
    timezone: str
    home_label: str
    office_label: str
    window_start: datetime | None = None
    window_end: datetime | None = None
    next_window_start: datetime | None = None
    days: list[str] = []
    slot_minutes: int = 15
    recommendation: Recommendation | None = None
    live: SlotView | None = None
    slots: list[SlotView] = []
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    requests_today: int = 0
    request_cap: int = 0
    monitor_running: bool = False


BASIS_LABELS = {
    "live": "Live traffic",
    "forecast": "Forecast · TomTom historical traffic patterns",
}


def _fmt_day(dt: datetime, now: datetime) -> str:
    if dt.date() == now.date():
        day = "today"
    elif dt.date() == now.date() + timedelta(days=1):
        day = "tomorrow"
    else:
        day = dt.strftime("%A")
    return f"{day} at {dt.strftime('%-I:%M %p')}"


# ---------------------------------------------------------------------------
# monitor
# ---------------------------------------------------------------------------

class CommuteMonitor:
    TICK_SECONDS = 60
    LIVE_FRESH = timedelta(minutes=25)
    MIN_LEAD = timedelta(minutes=2)       # slots this close to "now" are no longer options
    REQUEST_SPACING = 0.25                # seconds between requests in a batch

    def __init__(self, cfg: CommuteConfig, provider: TomTomProvider | None, store: CommuteStore,
                 *, lock_path: Path | None = None, on_change=None, clock=None, missing: str | None = None) -> None:
        self.cfg = cfg
        self.provider = provider
        self.store = store
        self.lock_path = lock_path
        self.on_change = on_change or (lambda: None)
        self.clock = clock or (lambda: datetime.now(cfg.tz))
        self.missing = missing          # why the monitor can't run (shown to the user)
        self._run_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._lock_file = None
        self._backoff_until: datetime | None = None
        self._auth_failed_on: date | None = None

    @property
    def configured(self) -> bool:
        return self.provider is not None and self.cfg.home is not None and self.cfg.office is not None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ----- lifecycle -----

    def _acquire_process_lock(self) -> bool:
        """Only one AURA process may run the monitor (avoids duplicate jobs)."""
        if self.lock_path is None:
            return True
        try:
            f = open(self.lock_path, "w")  # noqa: SIM115 - held for the process lifetime
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.warning("Commute monitor already running in another AURA process; not starting a second one")
            return False
        self._lock_file = f
        return True

    def start(self) -> bool:
        if not self.cfg.enabled:
            log.info("Commute monitor disabled (AURA_COMMUTE_ENABLED=false)")
            return False
        if not self.configured:
            log.info("Commute monitor not started: %s", self.missing or "not configured")
            return False
        if self.running:
            return True  # idempotent: never two loops in one process
        if self._lock_file is None and not self._acquire_process_lock():
            return False
        self._task = asyncio.create_task(self._loop(), name="aura-commute-monitor")
        c = self.cfg
        log.info("Commute monitor started: %s %s-%s %s, every %d min, max %d requests/day",
                 ",".join(c.days), c.window_start.strftime("%H:%M"), c.window_end.strftime("%H:%M"),
                 c.timezone, c.slot_minutes, c.max_requests_per_day)
        return True

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        if self._lock_file:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

    async def _loop(self) -> None:
        last_prune: date | None = None
        while True:
            try:
                await self.tick()
                today = self.clock().date()
                if last_prune != today:
                    self.store.prune(self.cfg.history_days, today)
                    last_prune = today
            except asyncio.CancelledError:
                raise
            except Exception:  # never let the monitor die silently
                log.exception("Commute monitor tick failed")
            await asyncio.sleep(self.TICK_SECONDS)

    # ----- one scheduling step (also used directly by tests) -----

    async def tick(self) -> list[str]:
        """Fetch whatever is due right now. Returns what was done (for logs/tests)."""
        if not self.cfg.enabled or not self.configured:
            return []
        now = self.clock()
        st = schedule_state(self.cfg, now)
        if st.status not in ("prefetch", "active"):
            return []
        if self._auth_failed_on == st.service_date:
            return ["skip:auth"]
        if self._backoff_until and now < self._backoff_until:
            return ["skip:backoff"]
        if self._run_lock.locked():
            return ["skip:busy"]  # never overlap
        async with self._run_lock:
            return await self._run(now, st)

    def _due(self, key: str, now: datetime, every: timedelta) -> bool:
        last = self.store.meta(key)
        return last is None or now - datetime.fromisoformat(last) >= every

    async def _fetch(self, depart_at: datetime | None, slot: str, st: ScheduleState, now: datetime) -> None:
        if self.store.requests(st.service_date) >= self.cfg.max_requests_per_day:
            raise CommuteProviderError("cap", f"Daily request limit reached ({self.cfg.max_requests_per_day})")
        self.store.count_request(st.service_date)
        est = await self.provider.route(self.cfg.home, self.cfg.office, depart_at)
        self.store.record(est, slot, st.service_date, now)
        self.store.set_meta("last_success_at", now.isoformat())

    async def _run(self, now: datetime, st: ScheduleState) -> list[str]:
        done: list[str] = []
        day = st.service_date.isoformat()
        try:
            if st.status == "active" and self._due(f"live:{day}", now, timedelta(minutes=self.cfg.live_refresh_minutes)):
                await self._fetch(None, "now", st, now)
                self.store.set_meta(f"live:{day}", now.isoformat())
                done.append("live")
            if self._due(f"forecast:{day}", now, timedelta(minutes=self.cfg.forecast_refresh_minutes)):
                upcoming = [s for s in slots(self.cfg, st.service_date) if s >= now + timedelta(minutes=5)]
                fetched = 0
                try:
                    for s in upcoming:
                        if fetched and self.REQUEST_SPACING:
                            await asyncio.sleep(self.REQUEST_SPACING)
                        await self._fetch(s, s.strftime("%H:%M"), st, now)
                        fetched += 1
                finally:
                    if fetched:
                        done.append(f"forecast:{fetched}")
                self.store.set_meta(f"forecast:{day}", now.isoformat())
                if not upcoming:
                    done.append("forecast:0")
            if done:
                self.store.set_meta("last_error", None)
        except CommuteProviderError as exc:
            self._error(now, str(exc))
            if exc.kind == "cap":
                pass  # stop for today; counter resets tomorrow
            elif exc.kind == "auth":
                self._auth_failed_on = st.service_date
            elif exc.kind == "rate_limit":
                self._backoff_until = now + timedelta(seconds=exc.retry_after or 1800)
            else:
                self._backoff_until = now + timedelta(minutes=5)
            done.append(f"error:{exc.kind}")
        if done:
            log.info("Commute check %s: %s (requests today %d/%d)", now.strftime("%a %H:%M"),
                     ", ".join(done), self.store.requests(st.service_date), self.cfg.max_requests_per_day)
            self.on_change()
        return done

    def _error(self, now: datetime, message: str) -> None:
        log.warning("Commute: %s", message)
        self.store.set_meta("last_error", message)
        self.store.set_meta("last_error_at", now.isoformat())

    # ----- view for the dashboard -----

    def status(self) -> CommuteStatus:
        cfg = self.cfg
        now = self.clock()
        base = dict(
            configured=self.configured, provider=self.provider.name if self.provider else None,
            timezone=cfg.timezone, home_label=cfg.home_label, office_label=cfg.office_label,
            days=list(cfg.days), slot_minutes=cfg.slot_minutes, request_cap=cfg.max_requests_per_day,
            monitor_running=self.running,
        )
        if not cfg.enabled:
            return CommuteStatus(state="disabled", message="Commute monitoring is turned off.", **base)
        if not self.configured:
            return CommuteStatus(state="unconfigured", message=self.missing or "Commute is not configured.", **base)

        st = schedule_state(cfg, now)
        last_ok = self.store.meta("last_success_at")
        last_err = self.store.meta("last_error")
        last_err_at = self.store.meta("last_error_at")
        base.update(
            window_start=st.window_start, window_end=st.window_end, next_window_start=st.next_window_start,
            last_success_at=last_ok, last_error=last_err, last_error_at=last_err_at,
            requests_today=self.store.requests(st.service_date),
        )
        observed = self.store.latest_for_day(st.service_date)
        start_txt = st.window_start.strftime("%-I:%M %p")
        end_txt = st.window_end.strftime("%-I:%M %p")
        nxt = _fmt_day(st.next_window_start - timedelta(minutes=cfg.prefetch_minutes), now)

        if st.status == "off_day":
            why = {"weekend": "It's the weekend", "holiday": "Today is a configured holiday"}.get(
                st.day_reason, "Not a commute day")
            return CommuteStatus(state="off_day", message=f"{why}: no commute monitoring. Next check {nxt}.", **base)
        if st.status == "after_window":
            slots_view = self._slot_views(st, now, observed, include_history=False, after=True)
            return CommuteStatus(
                state="after_window", slots=slots_view,
                message=f"Today's window ({start_txt}–{end_txt}) has ended. Next check {nxt}.", **base)

        views = self._slot_views(st, now, observed, include_history=True)
        live = None
        obs_now = observed.get("now")
        if st.status == "active" and obs_now and now - obs_now.observed_at <= self.LIVE_FRESH:
            live = self._view(obs_now, now, past=False)

        candidates = [v for v in views if not v.past and v.duration_min is not None]
        if live is not None:
            candidates.append(live)
        rec = None
        if candidates:
            # Shortest journey wins; ties go to the earliest departure.
            best = min(candidates, key=lambda v: (v.duration_min, v.departure_at))
            best.best = True
            rec = self._recommendation(best, live is best, st)

        if st.status == "before_window":
            msg = f"Monitoring starts {nxt}."
            if rec and rec.basis == "historical":
                msg = f"Typical pattern from past trips. Live checks start {nxt}."
        elif rec is None and last_err:
            msg = f"No traffic data yet: {last_err}."
        elif rec is None:
            msg = "Fetching traffic for today's departure options…"
        else:
            msg = f"Watching traffic {start_txt}–{end_txt}."
        return CommuteStatus(state=st.status, message=msg, recommendation=rec, live=live, slots=views, **base)

    def _view(self, o: Observation, now: datetime, past: bool) -> SlotView:
        return SlotView(
            departure_at=o.departure_at, arrival_at=o.arrival_at, duration_min=round(o.duration_s / 60),
            traffic_level=traffic_level(o.duration_s, o.no_traffic_s), basis=o.basis,
            delay_min=round(max(0, o.duration_s - o.no_traffic_s) / 60) if o.no_traffic_s else None,
            observed_at=o.observed_at, past=past,
        )

    def _slot_views(self, st: ScheduleState, now: datetime, observed: dict[str, Observation],
                    include_history: bool, after: bool = False) -> list[SlotView]:
        out = []
        for s in slots(self.cfg, st.service_date):
            key = s.strftime("%H:%M")
            past = after or s < now - self.MIN_LEAD
            o = observed.get(key)
            if o is not None:
                v = self._view(o, now, past)
                v.departure_at = s
                out.append(v)
                continue
            v = SlotView(departure_at=s, past=past)
            if include_history and not past:
                typ = self.store.typical(st.service_date.weekday(), s.time(), st.service_date)
                if typ:
                    secs, n = typ
                    v.duration_min, v.samples, v.basis = round(secs / 60), n, "historical"
                    v.arrival_at = s + timedelta(seconds=secs)
            out.append(v)
        return out

    def _recommendation(self, v: SlotView, leave_now: bool, st: ScheduleState) -> Recommendation:
        if v.basis == "historical":
            label = f"Typical for {st.service_date.strftime('%A')}s · from {v.samples} of your past trips"
        else:
            label = BASIS_LABELS[v.basis]
        delay = v.delay_min
        return Recommendation(
            departure_at=v.departure_at, arrival_at=v.arrival_at or v.departure_at + timedelta(minutes=v.duration_min),
            duration_min=v.duration_min, traffic_level=v.traffic_level, delay_min=delay,
            basis=v.basis, basis_label=label, leave_now=leave_now,
        )
