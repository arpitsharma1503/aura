"""Price Tracker: IBJA readings from a simulated metals.dev, purity and unit conversions,
day-on-day changes, the 12:00 schedule with retries and catch-up, quota guards, stale and
unavailable states. No real requests."""

import asyncio
from datetime import datetime, timedelta

import httpx
import pytest

from app.prices import IST, MONTHLY_CAP, PriceError, PriceTracker, derive, parse


def ok(gold=14163.0225, silver=194.4171):
    return {"status": "success", "authority": "ibja", "currency": "INR", "unit": "g",
            "timestamp": "2026-10-25T06:30:00.000Z", "rates": {"ibja_gold": gold, "ibja_silver": silver}}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def tracker(tmp_path, answers, clock, key="SECRET-KEY-123"):
    calls = []

    def handler(request):
        calls.append(request)
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return httpx.Response(200, json=a)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PriceTracker(tmp_path / "aura.db", key, client, now=clock), calls


def test_units():
    assert derive(14163.0225, 194.4171) == {"gold24": 141630, "silver": 194417}   # ₹ per 10 g; ₹ per kg (nearest rupee)


@pytest.mark.parametrize("payload, says", [
    ({"status": "failure", "error_message": "Invalid API key"}, "Invalid API key"),
    ({**ok(), "currency": "USD"}, "different market"),
    ({**ok(), "authority": "lbma"}, "different market"),
    (ok(gold=0), "doesn't look right"), (ok(silver=None), "doesn't look right"), (ok(gold=1e9), "doesn't look right"),
])
def test_odd_answers_are_refused(payload, says):
    with pytest.raises(PriceError) as e:
        parse(payload)
    assert says in str(e.value)


def test_daily_readings_changes_and_history(tmp_path):
    clock = Clock(datetime(2026, 10, 26, 12, 0, tzinfo=IST))
    t, calls = tracker(tmp_path, [ok(14000, 190), ok(14163.0225, 194.4171)], clock)
    v = asyncio.run(t.fetch())
    assert v["status"] == "ok" and all(i["change"] is None for i in v["items"])       # first reading: nothing to compare
    clock.t += timedelta(days=1)
    v = asyncio.run(t.fetch())
    g24 = next(i for i in v["items"] if i["id"] == "gold24")
    assert g24["change"] == 1630 and g24["change_pct"] == round(1630 / 140000 * 100, 2)
    assert g24["previous_day"] == "2026-10-26" and g24["unit"] == "10 g"
    assert [i["id"] for i in v["items"]] == ["gold24", "silver"] and g24["previous"] == 140000
    silver = next(i for i in v["items"] if i["id"] == "silver")
    assert silver["unit"] == "kg" and silver["change"] == 194417 - 190000
    assert [h["day"] for h in v["history"]] == ["2026-10-26", "2026-10-27"]
    assert "api_key=SECRET-KEY-123" in str(calls[0].url) and "authority=ibja" in str(calls[0].url)
    assert "SECRET-KEY-123" not in str(v)                                              # the key never reaches screens


def test_noon_schedule_retries_and_catch_up(tmp_path):
    clock = Clock(datetime(2026, 10, 26, 9, 0, tzinfo=IST))
    t, calls = tracker(tmp_path, [httpx.ConnectError("down"), ok()], clock)
    assert not t.due() and t.next_run() == datetime(2026, 10, 26, 12, 0, tzinfo=IST)
    clock.t = datetime(2026, 10, 26, 12, 0, tzinfo=IST)
    assert t.due() and t.next_run() == clock.t
    with pytest.raises(PriceError):
        asyncio.run(t.fetch())
    assert t.next_run() == datetime(2026, 10, 26, 12, 15, tzinfo=IST)                   # retry
    clock.t = datetime(2026, 10, 26, 12, 15, tzinfo=IST)
    asyncio.run(t.fetch())
    assert not t.due() and t.next_run() == datetime(2026, 10, 27, 12, 0, tzinfo=IST)    # tomorrow noon
    # the Pi was off at noon: catch up when it starts
    clock.t = datetime(2026, 10, 27, 15, 40, tzinfo=IST)
    t2, _ = tracker(tmp_path, [], clock)
    assert t2.due() and t2.next_run() == clock.t


def test_one_check_a_day_and_the_monthly_cap(tmp_path):
    clock = Clock(datetime(2026, 10, 26, 12, 0, tzinfo=IST))
    t, calls = tracker(tmp_path, [ok(), ok()], clock)
    asyncio.run(t.fetch())
    clock.t += timedelta(hours=6)
    with pytest.raises(PriceError) as e:                          # the same day: no second request
        asyncio.run(t.fetch())
    assert "already in" in str(e.value) and len(calls) == 1 and not t.due()
    t._db.execute("UPDATE metal_request SET n = ?", (MONTHLY_CAP,))
    clock.t = datetime(2026, 10, 27, 12, 0, tzinfo=IST)
    with pytest.raises(PriceError) as e:
        asyncio.run(t.fetch())
    assert "used up" in str(e.value) and len(calls) == 1


def test_states(tmp_path):
    clock = Clock(datetime(2026, 10, 26, 11, 0, tzinfo=IST))
    assert tracker(tmp_path / "a", [], clock, key=None)[0].view()["status"] == "not_configured"
    t, _ = tracker(tmp_path, [ok(), httpx.ConnectError("down")], clock)
    assert t.view()["status"] == "unavailable" and t.view()["items"] == []
    clock.t = datetime(2026, 10, 26, 12, 0, tzinfo=IST)
    asyncio.run(t.fetch())
    clock.t = datetime(2026, 10, 27, 11, 0, tzinfo=IST)                                  # before today's noon: still current
    assert t.view()["status"] == "ok"
    clock.t = datetime(2026, 10, 27, 14, 0, tzinfo=IST)                                  # noon passed, update failed
    with pytest.raises(PriceError):
        asyncio.run(t.fetch())
    v = t.view()
    assert v["status"] == "stale" and "2026-10-26" in v["message"] and v["items"]          # last price kept, clearly dated


@pytest.fixture(autouse=True)
def _tmp_dirs(tmp_path):
    (tmp_path / "a").mkdir()


def test_lower_purities_are_calculated_from_24k(tmp_path):
    from app.prices import purities
    assert purities(141630) == {"gold22": 128883, "gold20": 118020, "gold18": 106223, "gold14": 82613}   # x0.91, x0.8333, x0.75, x0.5833
    clock = Clock(datetime(2026, 10, 26, 12, 0, tzinfo=IST))
    t, _ = tracker(tmp_path, [ok(14000, 190), ok(14163.0225, 194.4171)], clock)
    asyncio.run(t.fetch())
    first = t.view()["gold_purities"]
    assert [p["karat"] for p in first] == ["22K", "20K", "18K", "14K"] and all(p["change"] is None for p in first)
    clock.t += timedelta(days=1)
    asyncio.run(t.fetch())
    g22 = t.view()["gold_purities"][0]
    assert g22 == {"id": "gold22", "karat": "22K", "factor": "0.91", "unit": "10 g", "price": 128883, "previous": 127400,
                   "change": 1483, "change_pct": round(1483 / 127400 * 100, 2)}      # 140000 x 0.91 = 127400
