import asyncio
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.commute import (
    CommuteConfig,
    CommuteMonitor,
    CommuteProviderError,
    CommuteStore,
    Point,
    TomTomProvider,
    next_window_start,
    schedule_state,
    slots,
    traffic_level,
)
from conftest import FakeNetwork, make_client

IST = ZoneInfo("Asia/Kolkata")
HOME = Point(13.0087307, 77.7381464)
OFFICE = Point(12.9927608, 77.6995722)
MONDAY = date(2026, 10, 19)       # a Monday
SATURDAY = date(2026, 10, 24)


def at(d: date, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=IST)


def cfg(**kw) -> CommuteConfig:
    return CommuteConfig(home=HOME, office=OFFICE, **kw)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def tomtom_body(depart: datetime, minutes: float, free_minutes: float = 22) -> dict:
    arrive = depart + timedelta(minutes=minutes)
    return {"routes": [{"summary": {
        "lengthInMeters": 9800,
        "travelTimeInSeconds": int(minutes * 60),
        "trafficDelayInSeconds": int((minutes - free_minutes) * 60),
        "noTrafficTravelTimeInSeconds": int(free_minutes * 60),
        "historicTrafficTravelTimeInSeconds": int(minutes * 60),
        "liveTrafficIncidentsTravelTimeInSeconds": int(minutes * 60),
        "departureTime": depart.isoformat(),
        "arrivalTime": arrive.isoformat(),
    }}]}


def tomtom(durations=None, live_minutes=40.0, clock: Clock | None = None):
    """Fake TomTom: duration per departure 'HH:MM', or a live value for departAt=now."""
    durations = durations or {}

    def handler(request: httpx.Request) -> httpx.Response:
        depart = request.url.params["departAt"]
        if depart == "now":
            return httpx.Response(200, json=tomtom_body(clock.now if clock else datetime.now(IST), live_minutes))
        dt = datetime.fromisoformat(depart)
        return httpx.Response(200, json=tomtom_body(dt, durations.get(dt.strftime("%H:%M"), 35)))

    return FakeNetwork({"api.tomtom.com": handler})


def monitor(tmp_path, net: FakeNetwork, clock: Clock, config: CommuteConfig | None = None,
            lock=True, provider=True) -> CommuteMonitor:
    client = httpx.AsyncClient(transport=net.transport)
    prov = TomTomProvider(client, "test-key") if provider else None
    m = CommuteMonitor(config or cfg(), prov, CommuteStore(tmp_path / "aura.db"),
                       lock_path=tmp_path / "commute.lock" if lock else None, clock=clock)
    m.REQUEST_SPACING = 0  # production spaces batch requests; tests needn't wait
    return m


def run(coro):
    return asyncio.run(coro)


# --- schedule -------------------------------------------------------------------

def test_slots_cover_7_to_11_every_15_minutes_in_ist():
    s = slots(cfg(), MONDAY)
    assert len(s) == 17
    assert s[0] == at(MONDAY, 7) and s[-1] == at(MONDAY, 11)
    assert all(x.utcoffset() == timedelta(hours=5, minutes=30) for x in s)
    assert all(b - a == timedelta(minutes=15) for a, b in zip(s, s[1:]))


@pytest.mark.parametrize("hh, mm, ss, expected", [
    (6, 44, 59, "before_window"),
    (6, 45, 0, "prefetch"),
    (6, 59, 59, "prefetch"),
    (7, 0, 0, "active"),
    (9, 30, 0, "active"),
    (11, 0, 0, "active"),
    (11, 0, 1, "after_window"),
    (23, 0, 0, "after_window"),
])
def test_window_boundaries(hh, mm, ss, expected):
    assert schedule_state(cfg(), at(MONDAY, hh, mm, ss)).status == expected


def test_timezone_is_applied_to_utc_clock():
    utc_7am_ist = datetime(2026, 10, 19, 1, 30, tzinfo=ZoneInfo("UTC"))   # 07:00 IST
    st = schedule_state(cfg(), utc_7am_ist)
    assert st.status == "active"
    assert st.window_start == at(MONDAY, 7)


def test_weekends_and_holidays_are_off():
    assert schedule_state(cfg(), at(SATURDAY, 8)).status == "off_day"
    assert schedule_state(cfg(), at(SATURDAY, 8)).day_reason == "weekend"
    holiday = cfg(holidays=frozenset({MONDAY}))
    st = schedule_state(holiday, at(MONDAY, 8))
    assert st.status == "off_day" and st.day_reason == "holiday"


def test_next_window_start():
    friday_noon = at(date(2026, 10, 23), 12)
    assert next_window_start(cfg(), friday_noon) == at(date(2026, 10, 26), 7)       # Monday
    holiday = cfg(holidays=frozenset({date(2026, 10, 26)}))
    assert next_window_start(holiday, friday_noon) == at(date(2026, 10, 27), 7)     # Tuesday
    assert next_window_start(cfg(), at(MONDAY, 5)) == at(MONDAY, 7)


@pytest.mark.parametrize("dur, free, level", [
    (1200, 1150, "light"), (1500, 1200, "moderate"), (1900, 1200, "heavy"), (2400, 1200, "severe"),
    (1200, None, None),
])
def test_traffic_level(dur, free, level):
    assert traffic_level(dur, free) == level


# --- provider ---------------------------------------------------------------------

def test_request_uses_exact_home_and_office_coordinates(tmp_path):
    clock = Clock(at(MONDAY, 8))
    net = tomtom(clock=clock)
    prov = TomTomProvider(httpx.AsyncClient(transport=net.transport), "k")
    est = run(prov.route(HOME, OFFICE, at(MONDAY, 9, 15)))
    req = net.calls[0]
    assert "/routing/1/calculateRoute/13.0087307,77.7381464:12.9927608,77.6995722/json" in req.url.path
    p = req.url.params
    assert p["traffic"] == "true" and p["computeTravelTimeFor"] == "all" and p["travelMode"] == "car"
    assert p["departAt"] == "2026-10-19T09:15:00+05:30"
    assert est.basis == "forecast" and est.duration_s == 35 * 60
    run(prov.route(HOME, OFFICE, None))
    assert net.calls[1].url.params["departAt"] == "now"


@pytest.mark.parametrize("response, kind", [
    ((403, {"detailedError": {"message": "Forbidden"}}), "auth"),
    ((429, {}), "rate_limit"),
    ((500, {}), "unavailable"),
    ((200, {"routes": []}), "bad_response"),
    ((200, {"routes": [{"summary": {"travelTimeInSeconds": 0}}]}), "bad_response"),
    (httpx.ReadTimeout("slow"), "timeout"),
    (httpx.ConnectError("down"), "unavailable"),
])
def test_provider_errors(response, kind):
    net = FakeNetwork({"api.tomtom.com": response})
    prov = TomTomProvider(httpx.AsyncClient(transport=net.transport), "k")
    with pytest.raises(CommuteProviderError) as exc:
        run(prov.route(HOME, OFFICE, None))
    assert exc.value.kind == kind
    assert "k" not in str(exc.value).split()  # key never in messages


# --- monitor ---------------------------------------------------------------------

def test_prefetch_gets_forecast_for_every_slot(tmp_path):
    clock = Clock(at(MONDAY, 6, 45))
    net = tomtom(clock=clock)
    m = monitor(tmp_path, net, clock)
    assert run(m.tick()) == ["forecast:17"]
    assert len(net.calls) == 17
    assert all(r.url.params["departAt"] != "now" for r in net.calls)   # no live check before the window
    assert m.store.requests(MONDAY) == 17


def test_active_window_live_and_remaining_slots(tmp_path):
    clock = Clock(at(MONDAY, 8))
    net = tomtom(clock=clock)
    m = monitor(tmp_path, net, clock)
    assert run(m.tick()) == ["live", "forecast:12"]    # 08:15 ... 11:00
    clock.now = at(MONDAY, 8, 1)
    assert run(m.tick()) == []                          # nothing due yet
    clock.now = at(MONDAY, 8, 15)
    assert run(m.tick()) == ["live"]                    # live every 15 min, forecast every 30
    clock.now = at(MONDAY, 8, 30)
    assert run(m.tick()) == ["live", "forecast:10"]


def test_no_requests_outside_window_or_on_weekends(tmp_path):
    for when in (at(MONDAY, 5), at(MONDAY, 11, 1), at(MONDAY, 18), at(SATURDAY, 8)):
        clock = Clock(when)
        net = tomtom(clock=clock)
        m = monitor(tmp_path, net, clock, lock=False)
        assert run(m.tick()) == []
        assert net.calls == []


def test_best_departure_is_shortest_future_slot(tmp_path):
    clock = Clock(at(MONDAY, 6, 45))
    net = tomtom({"09:30": 24, "10:00": 24, "08:00": 50}, clock=clock)
    m = monitor(tmp_path, net, clock)
    run(m.tick())
    st = m.status()
    rec = st.recommendation
    assert rec.departure_at == at(MONDAY, 9, 30)        # tie with 10:00 -> earliest
    assert rec.duration_min == 24
    assert rec.arrival_at == at(MONDAY, 9, 54)
    assert rec.basis == "forecast" and "historical traffic patterns" in rec.basis_label
    assert rec.traffic_level == "light"
    assert [s.best for s in st.slots].count(True) == 1
    assert st.state == "prefetch"


def test_past_slots_are_not_recommended(tmp_path):
    clock = Clock(at(MONDAY, 6, 45))
    net = tomtom({"07:00": 20}, clock=clock)
    m = monitor(tmp_path, net, clock)
    run(m.tick())
    assert m.status().recommendation.departure_at == at(MONDAY, 7)
    clock.now = at(MONDAY, 7, 30)
    st = m.status()
    assert st.recommendation.departure_at != at(MONDAY, 7)
    assert next(s for s in st.slots if s.departure_at == at(MONDAY, 7)).past


def test_leave_now_wins_when_live_is_fastest(tmp_path):
    clock = Clock(at(MONDAY, 8))
    net = tomtom(live_minutes=21, clock=clock)
    m = monitor(tmp_path, net, clock)
    run(m.tick())
    rec = m.status().recommendation
    assert rec.leave_now and rec.basis == "live" and rec.basis_label == "Live traffic"
    assert rec.duration_min == 21


def test_provider_failure_shows_no_numbers(tmp_path):
    clock = Clock(at(MONDAY, 8))
    net = FakeNetwork({"api.tomtom.com": httpx.ConnectError("down")})
    m = monitor(tmp_path, net, clock)
    assert run(m.tick()) == ["error:unavailable"]
    st = m.status()
    assert st.recommendation is None and st.live is None
    assert all(s.duration_min is None for s in st.slots)
    assert "unreachable" in st.message
    clock.now = at(MONDAY, 8, 3)
    assert run(m.tick()) == ["skip:backoff"]
    assert len(net.calls) == 1


def test_rate_limit_backs_off(tmp_path):
    clock = Clock(at(MONDAY, 8))
    net = FakeNetwork({"api.tomtom.com": (429, {})})
    m = monitor(tmp_path, net, clock)
    assert run(m.tick()) == ["error:rate_limit"]
    clock.now = at(MONDAY, 8, 20)
    assert run(m.tick()) == ["skip:backoff"]
    assert len(net.calls) == 1
    assert "rate limit" in m.status().last_error


def test_bad_key_stops_for_the_day(tmp_path):
    clock = Clock(at(MONDAY, 8))
    net = FakeNetwork({"api.tomtom.com": (403, {})})
    m = monitor(tmp_path, net, clock)
    assert run(m.tick()) == ["error:auth"]
    clock.now = at(MONDAY, 10)
    assert run(m.tick()) == ["skip:auth"]
    assert len(net.calls) == 1


def test_daily_request_cap(tmp_path):
    clock = Clock(at(MONDAY, 6, 45))
    net = tomtom(clock=clock)
    m = monitor(tmp_path, net, clock, cfg(max_requests_per_day=5))
    run(m.tick())
    assert len(net.calls) == 5
    assert "limit" in m.status().last_error


def test_missing_credentials(tmp_path):
    clock = Clock(at(MONDAY, 8))
    net = tomtom(clock=clock)
    m = monitor(tmp_path, net, clock, provider=False)
    m.missing = "Live traffic needs a free TomTom API key"
    assert run(m.tick()) == []
    assert net.calls == []
    st = m.status()
    assert st.state == "unconfigured" and "API key" in st.message
    assert st.recommendation is None and st.slots == []


def test_after_window_and_weekend_status_are_not_recommendations(tmp_path):
    clock = Clock(at(MONDAY, 10))
    m = monitor(tmp_path, tomtom(clock=clock), clock)
    run(m.tick())
    clock.now = at(MONDAY, 12)
    st = m.status()
    assert st.state == "after_window" and st.recommendation is None
    assert "has ended" in st.message and "tomorrow at 6:45 AM" in st.message
    assert all(s.past for s in st.slots)
    clock.now = at(SATURDAY, 9)
    st = m.status()
    assert st.state == "off_day" and st.recommendation is None
    assert "weekend" in st.message and "Monday at 6:45 AM" in st.message


def test_historical_pattern_from_own_observations(tmp_path):
    clock = Clock(at(MONDAY - timedelta(weeks=3), 8))
    net = tomtom(live_minutes=33, clock=clock)
    m = monitor(tmp_path, net, clock, lock=False)
    for week in (3, 2, 1):                               # three past Mondays, live check at 08:00
        clock.now = at(MONDAY - timedelta(weeks=week), 8)
        m.store.set_meta(f"live:{clock.now.date().isoformat()}", "2000-01-01T00:00:00+05:30")
        m.store.set_meta(f"forecast:{clock.now.date().isoformat()}", clock.now.isoformat())
        run(m.tick())
    clock.now = at(MONDAY, 5)                            # before today's window
    st = m.status()
    slot_8 = next(s for s in st.slots if s.departure_at == at(MONDAY, 8))
    assert slot_8.basis == "historical" and slot_8.duration_min == 33 and slot_8.samples == 3
    assert st.recommendation.basis == "historical"
    assert "from 3 of your past trips" in st.recommendation.basis_label


def test_restart_does_not_refetch_and_catches_up_missed_runs(tmp_path):
    clock = Clock(at(MONDAY, 9))
    net = tomtom(clock=clock)
    first = monitor(tmp_path, net, clock, lock=False)
    assert run(first.tick()) == ["live", "forecast:8"]  # missed run at 9:00 handled immediately (9:15-11:00)
    first.store.close()
    clock.now = at(MONDAY, 9, 5)
    second = monitor(tmp_path, net, clock, lock=False)  # "restarted" app, same database
    assert run(second.tick()) == []
    assert second.status().recommendation is not None   # previous results still shown


def test_no_duplicate_jobs(tmp_path):
    async def scenario():
        clock = Clock(at(MONDAY, 8))
        a = monitor(tmp_path, tomtom(clock=clock), clock)
        b = monitor(tmp_path, tomtom(clock=clock), clock)
        a.TICK_SECONDS = b.TICK_SECONDS = 3600
        assert a.start() is True
        task = a._task
        assert a.start() is True and a._task is task      # idempotent within the process
        assert b.start() is False                          # other process: lock held
        await a.stop()
        assert b.start() is True                           # lock released on stop
        await b.stop()

    run(scenario())


def test_ticks_never_overlap(tmp_path):
    async def scenario():
        clock = Clock(at(MONDAY, 8))
        gate = asyncio.Event()

        async def slow(request):
            await gate.wait()
            return httpx.Response(200, json=tomtom_body(clock.now, 30))

        class SlowTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return await slow(request)

        prov = TomTomProvider(httpx.AsyncClient(transport=SlowTransport()), "k")
        m = CommuteMonitor(cfg(), prov, CommuteStore(tmp_path / "aura.db"), clock=clock)
        first = asyncio.create_task(m.tick())
        await asyncio.sleep(0.01)
        assert await m.tick() == ["skip:busy"]
        gate.set()
        assert (await first)[0] == "live"

    run(scenario())


# --- API ---------------------------------------------------------------------------

def test_api_unconfigured_without_key(tmp_path):
    c = make_client(tmp_path, commute_home="13.0087307,77.7381464", commute_office="12.9927608,77.6995722")
    body = c.get("/api/commute").json()
    assert body["state"] == "unconfigured"
    assert "TomTom API key" in body["message"]
    assert body["recommendation"] is None


def test_api_never_exposes_coordinates_or_key(tmp_path):
    c = make_client(tmp_path, commute_home="13.0087307,77.7381464", commute_office="12.9927608,77.6995722",
                    tomtom_api_key="secret-tomtom-key", location_detection="off")
    text = c.get("/api/commute").text
    for leak in ("13.0087", "77.7381", "12.9927", "77.6995", "secret-tomtom-key"):
        assert leak not in text
    assert c.get("/api/commute").json()["home_label"] == "Home"


def test_settings_reject_bad_coordinates(tmp_path):
    with pytest.raises(ValueError):
        make_client(tmp_path, commute_home="13.0,200")
    with pytest.raises(ValueError):
        make_client(tmp_path, commute_window_start="7am")


def test_flat_forecast_does_not_beat_real_traffic_history(tmp_path):
    """Seen live: TomTom's forecast for later departures is nearly flat (20-21 min all morning)
    while AURA's own live checks rise to 25-29 min after 9:00, so "best" was always the earliest
    slot. A slot is ranked by the higher of its forecast and the median of past live checks
    (any weekday, at least 3), so a too-rosy forecast can't win."""
    from app.commute import RouteEstimate
    clock = Clock(at(MONDAY, 6, 45))
    flat = {f"{h:02d}:{m:02d}": 20 for h in range(7, 12) for m in (0, 15, 30, 45)}
    m = monitor(tmp_path, tomtom(flat, clock=clock), clock)
    for days_ago in (1, 3, 4):                                    # past weekdays' live checks (Fri, Thu, Wed)
        d = MONDAY - timedelta(days=days_ago)
        for hh, mm, minutes in ((7, 0, 30), (7, 15, 29), (7, 30, 19)):
            dep = at(d, hh, mm)
            m.store.record(RouteEstimate(departure_at=dep, arrival_at=dep + timedelta(minutes=minutes),
                                         duration_s=minutes * 60, basis="live", provider="tomtom"),
                           "now", d, dep)
    run(m.tick())
    st = m.status()
    by = {s.departure_at.strftime("%H:%M"): s for s in st.slots}
    assert by["07:00"].duration_min == 30 and by["07:00"].basis == "historical"   # history overrides the flat 20
    assert by["07:30"].duration_min == 20                                         # forecast higher than history: kept
    assert st.recommendation.departure_at == at(MONDAY, 7, 30)                    # not the earliest (07:00)


def test_best_is_ranked_on_seconds_not_rounded_minutes(tmp_path):
    """20.4 min and 19.6 min both show as "20": the faster one must still win (rounding
    made the earliest slot win every tie)."""
    clock = Clock(at(MONDAY, 6, 45))
    times = {f"{h:02d}:{m:02d}": 25 for h in range(7, 12) for m in (0, 15, 30, 45)}
    times.update({"07:00": 20.4, "08:30": 19.6})
    m = monitor(tmp_path, tomtom(times, clock=clock), clock)
    run(m.tick())
    rec = m.status().recommendation
    assert rec.departure_at == at(MONDAY, 8, 30) and rec.duration_min == 20
