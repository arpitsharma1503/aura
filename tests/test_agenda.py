"""Calendars: parsing, repeating events, one-per-day de-duplication, feeds, privacy."""

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.agenda import AgendaService, Feed, build_days, expand, norm_title, parse_feeds
from conftest import make_client

IST = ZoneInfo("Asia/Kolkata")
TODAY = date(2026, 10, 19)          # a Monday
SECRET = "private-0123456789abcdef"


def cal(*events: str, name: str = "Personal") -> str:
    body = "\r\n".join(events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nX-WR-CALNAME:{name}\r\n{body}\r\nEND:VCALENDAR\r\n"


def ev(uid, summary, start, end=None, extra=""):
    lines = [f"BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{summary}", start]
    if end:
        lines.append(end)
    if extra:
        lines += extra.split("\n")
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


PERSONAL = cal(
    ev("b1", "Maa's birthday", "DTSTART;VALUE=DATE:19650420", "DTEND;VALUE=DATE:19650421", "RRULE:FREQ=YEARLY"),
    ev("b2", "Rohan's Birthday", "DTSTART;VALUE=DATE:19901019", "DTEND;VALUE=DATE:19901020", "RRULE:FREQ=YEARLY"),
    ev("d1", "Diwali", "DTSTART;VALUE=DATE:20261108", "DTEND;VALUE=DATE:20261109"),
    ev("d2", "Diwali ", "DTSTART;VALUE=DATE:20261108", "DTEND;VALUE=DATE:20261109"),        # duplicate in same feed
    ev("t1", "Trip to Goa", "DTSTART;VALUE=DATE:20261021", "DTEND;VALUE=DATE:20261024"),      # 3 days
    ev("t0", "Conference", "DTSTART;VALUE=DATE:20261017", "DTEND;VALUE=DATE:20261021"),       # started before today
    ev("w1", "Team sync", "DTSTART;TZID=Asia/Kolkata:20261012T100000", "DTEND;TZID=Asia/Kolkata:20261012T103000",
       "RRULE:FREQ=WEEKLY;BYDAY=MO\nEXDATE;TZID=Asia/Kolkata:20261026T100000"),
    ev("w1", "Team sync", "DTSTART;TZID=Asia/Kolkata:20261102T150000", "DTEND;TZID=Asia/Kolkata:20261102T153000",
       "RECURRENCE-ID;TZID=Asia/Kolkata:20261102T100000"),
    ev("u1", "Call with London", "DTSTART;TZID=Europe/London:20261019T090000", "DTEND;TZID=Europe/London:20261019T100000"),
    ev("z1", "Dentist", "DTSTART:20261020T043000Z", "DTEND:20261020T051500Z"),
    ev("c1", "Cancelled thing", "DTSTART;VALUE=DATE:20261020", "DTEND;VALUE=DATE:20261021", "STATUS:CANCELLED"),
)
FAMILY = cal(
    ev("f1", "diwali", "DTSTART;VALUE=DATE:20261108", "DTEND;VALUE=DATE:20261109"),          # same day, other calendar
    ev("f2", "Rohan’s birthday", "DTSTART;VALUE=DATE:20261019", "DTEND;VALUE=DATE:20261020"),
    name="Family",
)
HOLIDAYS = cal(
    ev("h1", "Diwali/Deepavali", "DTSTART;VALUE=DATE:20261108", "DTEND;VALUE=DATE:20261109", "DESCRIPTION:Public holiday"),
    ev("h2", "Halloween", "DTSTART;VALUE=DATE:20261031", "DTEND;VALUE=DATE:20261101",
       "DESCRIPTION:Observance\\nTo hide observances, go to Google Calendar Settings"),
    name="Holidays in India",
)

FEEDS = [Feed(label="Personal", url=f"https://calendar.google.com/calendar/ical/me/{SECRET}/basic.ics"),
         Feed(label="Family", url="https://calendar.google.com/calendar/ical/family/private-x/basic.ics"),
         Feed(label="Holidays", url="https://calendar.google.com/calendar/ical/en.indian%23holiday/public/basic.ics")]
TEXTS = {"Personal": PERSONAL, "Family": FAMILY, "Holidays": HOLIDAYS}


def all_days(days=40):
    occ = []
    for f in FEEDS:
        occ += expand(TEXTS[f.label], f, TODAY, TODAY + timedelta(days=days), IST)
    return build_days(occ, TODAY, days)


def titles(day):
    return [i.title for i in day.items]


def find(days, d):
    return next((x for x in days if x.date == d), None)


# --- expansion ---------------------------------------------------------------------------

def test_yearly_birthday_is_a_birthday_once_per_year():
    days = all_days(200)
    rohan = find(days, TODAY)
    assert [i for i in rohan.items if norm_title(i.title) == norm_title("Rohan's Birthday")][0].kind == "birthday"
    maa = find(days, date(2027, 4, 20))
    assert maa and maa.items[0].kind == "birthday"


def test_weekly_series_respects_skipped_and_moved_occurrences():
    days = all_days(30)
    mondays = {d.date: [i for i in d.items if i.title == "Team sync"] for d in days}
    assert mondays[date(2026, 10, 19)][0].start == datetime(2026, 10, 19, 10, tzinfo=IST)
    assert not mondays.get(date(2026, 10, 26))                                   # EXDATE
    moved = mondays[date(2026, 11, 2)][0]
    assert moved.start.hour == 15                                                # RECURRENCE-ID override
    assert len(mondays[date(2026, 11, 9)]) == 1


def test_timezones_are_converted_to_home_time():
    today = find(all_days(), TODAY)
    london = next(i for i in today.items if i.title == "Call with London")
    assert london.start.astimezone(IST).strftime("%H:%M") == "13:30"
    dentist = next(i for i in find(all_days(), date(2026, 10, 20)).items if i.title == "Dentist")
    assert dentist.start.strftime("%H:%M") == "10:00"                           # 04:30Z -> 10:00 IST


def test_cancelled_events_are_hidden():
    assert "Cancelled thing" not in titles(find(all_days(), date(2026, 10, 20)))


def test_holiday_feed_kinds():
    days = all_days()
    assert find(days, date(2026, 10, 31)).items[0].kind == "observance"
    diwali_day = find(days, date(2026, 11, 8))
    assert any(i.kind == "holiday" for i in diwali_day.items)


# --- no repeats ---------------------------------------------------------------------------

def test_same_title_on_same_day_appears_once_across_calendars():
    day = find(all_days(), date(2026, 11, 8))
    diwali = [i for i in day.items if norm_title(i.title) == "diwali"]
    assert len(diwali) == 1                                    # Personal x2 + Family -> one
    assert diwali[0].calendars == ["Family", "Personal"]
    assert "Diwali/Deepavali" in titles(day)                   # a differently named holiday stays


def test_birthday_in_two_calendars_shows_once():
    day = find(all_days(), TODAY)
    births = [i for i in day.items if i.kind == "birthday"]
    assert len(births) == 1 and set(births[0].calendars) == {"Personal", "Family"}


def test_multi_day_event_listed_once_with_range():
    days = all_days()
    trips = [(d.date, i) for d in days for i in d.items if i.title == "Trip to Goa"]
    assert len(trips) == 1
    shown, trip = trips[0]
    assert shown == date(2026, 10, 21) and trip.last_day == date(2026, 10, 23)


def test_event_that_started_before_today_shows_once_today():
    days = all_days()
    conf = [(d.date, i) for d in days for i in d.items if i.title == "Conference"]
    assert len(conf) == 1 and conf[0][0] == TODAY and conf[0][1].last_day == date(2026, 10, 20)


def test_no_day_ever_repeats_a_title():
    for d in all_days(365):
        keys = [norm_title(i.title) for i in d.items]
        assert len(keys) == len(set(keys)), d


def test_order_all_day_first_then_by_time():
    day = find(all_days(), TODAY)
    kinds = [(i.all_day, i.kind) for i in day.items]
    assert kinds[0] == (True, "birthday")
    timed = [i.start for i in day.items if not i.all_day]
    assert timed == sorted(timed)


# --- feeds, caching, failures ---------------------------------------------------------------

def handler_for(texts, fail=()):
    def handler(request: httpx.Request) -> httpx.Response:
        for f in FEEDS:
            if f.url == str(request.url):
                if f.label in fail:
                    raise httpx.ConnectError("down")
                return httpx.Response(200, text=texts[f.label])
        return httpx.Response(404)
    return handler


def service(tmp_path, handler, feeds=FEEDS):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AgendaService(feeds, tmp_path / "calendar_cache.json", client, timezone="Asia/Kolkata",
                         clock=lambda: datetime(2026, 10, 19, 8, tzinfo=IST))


def test_refresh_and_agenda(tmp_path):
    svc = service(tmp_path, handler_for(TEXTS))
    assert asyncio.run(svc.refresh(force=True)) is True
    a = svc.agenda()
    assert a.status == "ok" and a.today == TODAY
    assert [f.label for f in a.feeds] == ["Personal", "Family", "Holidays"] and all(f.ok for f in a.feeds)
    assert a.days[0].date == TODAY


def test_failed_feed_keeps_last_good_copy(tmp_path):
    svc = service(tmp_path, handler_for(TEXTS))
    asyncio.run(svc.refresh(force=True))
    later = service(tmp_path, handler_for(TEXTS, fail={"Family"}))          # restart + Family down
    asyncio.run(later.refresh(force=True))
    a = later.agenda()
    assert a.status == "partial" and "last saved copy" in a.message
    fam = next(f for f in a.feeds if f.label == "Family")
    assert not fam.ok and fam.error == "unreachable"
    assert any("Family" in i.calendars for d in a.days for i in d.items)   # still shown from cache


def test_bad_feed_content_rejected(tmp_path):
    texts = {**TEXTS, "Holidays": "<html>Sign in</html>"}
    svc = service(tmp_path, handler_for(texts))
    asyncio.run(svc.refresh(force=True))
    hol = next(f for f in svc.agenda().feeds if f.label == "Holidays")
    assert not hol.ok and "iCalendar" in hol.error


def test_oversized_feed_rejected(tmp_path):
    texts = {**TEXTS, "Holidays": "BEGIN:VCALENDAR\r\n" + "X" * 6_000_000}
    svc = service(tmp_path, handler_for(texts))
    asyncio.run(svc.refresh(force=True))
    assert next(f for f in svc.agenda().feeds if f.label == "Holidays").error == "feed too large"


def test_nothing_reachable_is_an_error_state(tmp_path):
    svc = service(tmp_path, handler_for(TEXTS, fail={"Personal", "Family", "Holidays"}))
    asyncio.run(svc.refresh(force=True))
    a = svc.agenda()
    assert a.status == "error" and a.days == []


def test_refresh_respects_interval(tmp_path):
    calls = []

    def counting(request):
        calls.append(request.url)
        return handler_for(TEXTS)(request)

    svc = service(tmp_path, counting)
    asyncio.run(svc.refresh(force=True))
    asyncio.run(svc.refresh())
    assert len(calls) == 3                                                   # second call was cached


# --- configuration & privacy ---------------------------------------------------------------

def test_parse_feeds():
    feeds = parse_feeds(" Personal|https://a.example/x.ics , https://b.example/y.ics")
    assert [(f.label, f.url) for f in feeds] == [("Personal", "https://a.example/x.ics"), ("Calendar 2", "https://b.example/y.ics")]
    assert parse_feeds("") == []
    with pytest.raises(ValueError):
        parse_feeds("Bad|ftp://x/y.ics")
    with pytest.raises(ValueError):
        parse_feeds("Bad|file:///etc/passwd")


def test_secret_urls_never_leave_the_server(tmp_path, caplog):
    raw = ",".join(f"{f.label}|{f.url}" for f in FEEDS)
    net_calls = []

    def handler(request):
        net_calls.append(str(request.url))
        return handler_for(TEXTS, fail={"Family"})(request)

    c = make_client(tmp_path, calendar_feeds=raw)
    c.app.state.agenda.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.INFO):
        asyncio.run(c.app.state.agenda.refresh(force=True))
    body = c.get("/api/calendar").text + c.get("/api/settings").text + c.get("/api/config").text
    assert SECRET not in body and "calendar.google.com" not in body
    aura_logs = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("aura"))
    assert "Family" in aura_logs and SECRET not in aura_logs                 # logs: label + fingerprint only
    cache = (tmp_path / "calendar_cache.json").read_text()
    assert SECRET not in cache.split('"ics"')[0]                              # keys are fingerprints
    assert all(k == "ics" or k == "fetched_at" for v in json.loads(cache).values() for k in v)


def test_api_unconfigured(client):
    body = client.get("/api/calendar").json()
    assert body["status"] == "unconfigured" and body["days"] == []


def test_production_logging_silences_request_urls():
    src = (__import__("pathlib").Path(__file__).parent.parent / "app" / "__main__.py").read_text()
    assert 'logging.getLogger("httpx").setLevel(logging.WARNING)' in src    # httpx logs full URLs at INFO
