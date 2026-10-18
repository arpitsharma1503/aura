"""Calendars: events, birthdays and holidays from iCalendar (.ics) feeds.

* Feeds come from ``AURA_CALENDAR_FEEDS`` in ``.env`` ("Label|https://..." ,
  comma-separated). Private Google Calendar addresses are secrets: they are
  never logged in full, never returned by the API and never sent to the
  browser - only the labels are.
* Feeds are fetched on the server every ``refresh_minutes`` with a timeout and
  a size limit; the last good copy is kept (in memory and in
  ``data/calendar_cache.json``) when a feed is temporarily unreachable.
* Repeating events (RRULE, EXDATE, moved occurrences, time zones) are expanded
  with the ``recurring-ical-events`` library.
* No repeats: on any day, an item with the same (normalised) title appears
  only once, even if it is in several calendars or overlaps itself; an event
  spanning several days is listed once, on its first day in the window, with
  its date range.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events
from pydantic import BaseModel

from .storage import write_json_atomic

log = logging.getLogger("aura.calendar")

MAX_FEED_BYTES = 5_000_000
Kind = Literal["birthday", "event", "holiday", "observance"]
KIND_ORDER = {"birthday": 0, "event": 1, "holiday": 2, "observance": 3}
BIRTHDAY_RE = re.compile(r"\b(birthday|bday|b'day|anniversary)\b", re.I)


class Feed(BaseModel):
    label: str
    url: str

    @property
    def safe_name(self) -> str:
        """For logs: host + a short fingerprint, never the secret path."""
        host = urlparse(self.url).hostname or "?"
        return f"{self.label} ({host}, #{hashlib.sha256(self.url.encode()).hexdigest()[:6]})"


def parse_feeds(raw: str | None) -> list[Feed]:
    feeds: list[Feed] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        label, sep, url = part.partition("|")
        if not sep:
            label, url = f"Calendar {len(feeds) + 1}", part
        url = url.strip()
        if urlparse(url).scheme not in ("https", "http") or not urlparse(url).hostname:
            raise ValueError(f"calendar feed '{label.strip()}' must be an http(s) address")
        feeds.append(Feed(label=label.strip()[:30] or f"Calendar {len(feeds) + 1}", url=url))
    return feeds


class Occurrence(BaseModel):
    title: str
    start: datetime | None = None      # timed events (local time)
    end: datetime | None = None
    first_day: date
    last_day: date                     # inclusive; == first_day for one-day items
    all_day: bool
    kind: Kind
    calendars: list[str]


class FeedStatus(BaseModel):
    label: str
    ok: bool
    events: int = 0
    last_success: datetime | None = None
    error: str | None = None


class DayAgenda(BaseModel):
    date: date
    items: list[Occurrence]


class AgendaResponse(BaseModel):
    status: Literal["ok", "partial", "unconfigured", "error", "loading"]
    message: str | None = None
    timezone: str
    today: date
    days: list[DayAgenda] = []
    feeds: list[FeedStatus] = []
    updated_at: datetime | None = None


def norm_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", title).casefold()
    t = re.sub(r"[\W_]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def classify(title: str, description: str, feed_label: str, is_holiday_feed: bool) -> Kind:
    if is_holiday_feed:
        return "observance" if description.lower().startswith("observance") else "holiday"
    if BIRTHDAY_RE.search(title):
        return "birthday"
    return "event"


def expand(ics_text: str, feed: Feed, start: date, end: date, tz: ZoneInfo) -> list[Occurrence]:
    """All occurrences of a feed overlapping [start, end) in local days."""
    cal = icalendar.Calendar.from_ical(ics_text)
    holiday_feed = "holiday" in (str(cal.get("X-WR-CALNAME", "")) + feed.url).lower()
    out: list[Occurrence] = []
    for comp in recurring_ical_events.of(cal).between(start, end):
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue
        title = str(comp.get("SUMMARY", "")).strip() or "Busy"
        desc = str(comp.get("DESCRIPTION", ""))
        dtstart = comp.get("DTSTART").dt
        dtend_prop = comp.get("DTEND")
        dtend = dtend_prop.dt if dtend_prop is not None else None
        if isinstance(dtstart, datetime):
            s = dtstart if dtstart.tzinfo else dtstart.replace(tzinfo=tz)
            s = s.astimezone(tz)
            e = (dtend if isinstance(dtend, datetime) else s + timedelta(hours=1))
            e = (e if e.tzinfo else e.replace(tzinfo=tz)).astimezone(tz)
            last = (e - timedelta(seconds=1)).date() if e > s else s.date()
            out.append(Occurrence(title=title, start=s, end=e, first_day=s.date(), last_day=max(last, s.date()),
                                  all_day=False, kind=classify(title, desc, feed.label, holiday_feed),
                                  calendars=[feed.label]))
        else:  # all-day: DTEND is exclusive
            first = dtstart
            last = (dtend - timedelta(days=1)) if isinstance(dtend, date) and dtend > dtstart else dtstart
            out.append(Occurrence(title=title, first_day=first, last_day=last, all_day=True,
                                  kind=classify(title, desc, feed.label, holiday_feed), calendars=[feed.label]))
    return out


def build_days(occurrences: list[Occurrence], today: date, days: int) -> list[DayAgenda]:
    """Group into days without repeats (see module docstring)."""
    window_end = today + timedelta(days=days)
    by_day: dict[date, dict[str, Occurrence]] = {}
    seen_spans: set[tuple[str, date, date]] = set()
    for occ in sorted(occurrences, key=lambda o: (o.first_day, o.start or datetime.min.replace(tzinfo=UTC))):
        if occ.last_day < today or occ.first_day >= window_end:
            continue
        key = norm_title(occ.title)
        # Multi-day items: once, on the first day that is inside the window.
        shown_day = max(occ.first_day, today)
        span = (key, occ.first_day, occ.last_day)
        if occ.last_day > occ.first_day:
            if span in seen_spans:
                continue
            seen_spans.add(span)
        bucket = by_day.setdefault(shown_day, {})
        existing = bucket.get(key)
        if existing is None:
            bucket[key] = occ.model_copy()
        else:
            # Same title on the same day (other calendar / overlapping repeat): show once.
            existing.calendars = sorted(set(existing.calendars) | set(occ.calendars))
            if KIND_ORDER[occ.kind] < KIND_ORDER[existing.kind]:
                existing.kind = occ.kind
            if existing.start and occ.start and occ.start < existing.start:
                existing.start, existing.end = occ.start, occ.end
    out = []
    for d in sorted(by_day):
        # All-day first (birthdays, events, holidays, observances), then timed items by start time.
        items = sorted(by_day[d].values(), key=lambda o: (0, KIND_ORDER[o.kind], o.title.casefold())
                       if o.all_day else (1, o.start.timestamp(), o.title.casefold()))
        out.append(DayAgenda(date=d, items=items))
    return out


class AgendaService:
    def __init__(self, feeds: list[Feed], cache_path: Path, client: httpx.AsyncClient, *,
                 timezone: str, refresh_minutes: int = 30, days_ahead: int = 30, clock=None) -> None:
        self.feeds = feeds
        self.cache_path = cache_path
        self.client = client
        self.tz_name = timezone
        self.refresh_s = refresh_minutes * 60
        self.days_ahead = days_ahead
        self.clock = clock or (lambda: datetime.now(ZoneInfo(self.tz_name)))
        self._texts: dict[str, str] = {}          # feed fingerprint -> last good ics text
        self._status: dict[str, FeedStatus] = {f.label: FeedStatus(label=f.label, ok=False) for f in feeds}
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.on_change = lambda: None
        self._load_cache()

    @staticmethod
    def _fp(feed: Feed) -> str:
        return hashlib.sha256(feed.url.encode()).hexdigest()

    def _load_cache(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return
        for f in self.feeds:
            entry = data.get(self._fp(f))
            if isinstance(entry, dict) and isinstance(entry.get("ics"), str):
                self._texts[self._fp(f)] = entry["ics"]
                ts = entry.get("fetched_at")
                self._status[f.label] = FeedStatus(label=f.label, ok=True,
                                                   last_success=datetime.fromisoformat(ts) if ts else None)

    def _save_cache(self) -> None:
        data = {}
        for f in self.feeds:
            fp = self._fp(f)
            if fp in self._texts:
                st = self._status[f.label]
                data[fp] = {"ics": self._texts[fp],
                            "fetched_at": st.last_success.isoformat() if st.last_success else None}
        try:
            write_json_atomic(self.cache_path, data)   # keyed by fingerprint: no URLs on disk here
        except OSError:
            log.exception("Could not write calendar cache")

    async def _fetch(self, feed: Feed) -> str:
        async with self.client.stream("GET", feed.url, headers={"Accept": "text/calendar"}) as res:
            if res.status_code != 200:
                raise RuntimeError(f"HTTP {res.status_code}")
            chunks, size = [], 0
            async for chunk in res.aiter_bytes():
                size += len(chunk)
                if size > MAX_FEED_BYTES:
                    raise RuntimeError("feed too large")
                chunks.append(chunk)
        text = b"".join(chunks).decode("utf-8", errors="replace")
        if "BEGIN:VCALENDAR" not in text[:2000]:
            raise RuntimeError("not an iCalendar feed")
        icalendar.Calendar.from_ical(text)  # validate before replacing the last good copy
        return text

    async def refresh(self, force: bool = False) -> bool:
        if not self.feeds:
            return False
        async with self._lock:
            if not force and time.monotonic() - self._fetched_at < self.refresh_s and self._fetched_at:
                return False
            changed = False
            results = await asyncio.gather(*(self._fetch(f) for f in self.feeds), return_exceptions=True)
            now = datetime.now(UTC)
            for feed, res in zip(self.feeds, results):
                st = self._status[feed.label]
                if isinstance(res, BaseException):
                    msg = "timed out" if isinstance(res, httpx.TimeoutException) else (
                        "unreachable" if isinstance(res, httpx.HTTPError) else str(res)[:80])
                    st.ok, st.error = False, msg
                    log.warning("calendar: %s failed: %s", feed.safe_name, msg)
                    continue
                fp = self._fp(feed)
                if self._texts.get(fp) != res:
                    changed = True
                self._texts[fp] = res
                st.ok, st.error, st.last_success = True, None, now
            self._fetched_at = time.monotonic()
            if changed:
                self._save_cache()
                log.info("calendar: refreshed %d feed(s)", sum(s.ok for s in self._status.values()))
            return changed

    async def run(self) -> None:
        while True:
            try:
                if await self.refresh():
                    self.on_change()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("calendar refresh failed")
            await asyncio.sleep(60)

    def start(self) -> None:
        if self.feeds and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self.run(), name="aura-calendar")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def agenda(self, days: int | None = None) -> AgendaResponse:
        tz = ZoneInfo(self.tz_name)
        now = self.clock().astimezone(tz)
        today = now.date()
        days = min(days or self.days_ahead, 90)
        base = dict(timezone=self.tz_name, today=today, feeds=list(self._status.values()))
        if not self.feeds:
            return AgendaResponse(status="unconfigured", message="No calendars connected.", **base)
        occurrences: list[Occurrence] = []
        for f in self.feeds:
            text = self._texts.get(self._fp(f))
            if text is None:
                continue
            try:
                found = expand(text, f, today, today + timedelta(days=days), tz)
            except Exception as exc:   # a malformed feed must not break the others
                log.warning("calendar: could not read %s: %s", f.safe_name, exc.__class__.__name__)
                st = self._status[f.label]
                st.ok, st.error = False, "could not read the calendar"
                continue
            self._status[f.label].events = len(found)
            occurrences += found
        if not self._texts:
            failed = [s for s in self._status.values() if s.error]
            return AgendaResponse(status="error" if failed else "loading",
                                  message="Calendars couldn't be reached." if failed else "Loading calendars…", **base)
        last = max((s.last_success for s in self._status.values() if s.last_success), default=None)
        partial = any(not s.ok for s in self._status.values())
        return AgendaResponse(
            status="partial" if partial else "ok",
            message="Some calendars couldn't be updated; showing their last saved copy." if partial else None,
            days=build_days(occurrences, today, days), updated_at=last, **base)
