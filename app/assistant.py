"""AURA assistant: turns a question (typed or transcribed) into a grounded answer.

- Understanding is a fixed allowlist of intents matched by keywords; there is no
  language model and nothing here can run commands or change data.
- Every answer is built from AURA's own data (weather, commute, calendar,
  groceries). When that data is missing or stale the answer says so.
- New capabilities are added by registering another Tool in TOOLS.
"""

from __future__ import annotations

import itertools
import re
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from .agenda import AgendaResponse
from .commute import CommuteStatus
from .groceries import GroceryList
from .weather import WeatherResponse

MAX_QUESTION = 300
HISTORY = 20


# --------------------------------------------------------------------------- data access

@dataclass
class Sources:
    """What the tools may read. main.py wires these to the running services;
    tests pass fakes. All are read-only."""
    weather: Callable[[], Awaitable[WeatherResponse]]
    commute: Callable[[], CommuteStatus]
    calendar: Callable[[int], AgendaResponse]
    groceries: Callable[[], GroceryList]
    timezone: Callable[[], str]
    clock_format: Callable[[], str] = lambda: "12h"
    temperature_unit: Callable[[], str] = lambda: "celsius"
    travel_window_hours: Callable[[], int] = lambda: 6
    now: Callable[[ZoneInfo], datetime] = lambda tz: datetime.now(tz)


@dataclass
class Ctx:
    src: Sources
    tz: ZoneInfo
    now: datetime
    question: str

    def t(self, dt: datetime) -> str:
        dt = dt.astimezone(self.tz)
        if self.src.clock_format() == "24h":
            return dt.strftime("%H:%M")
        return dt.strftime("%-I:%M %p").replace(":00 ", " ")

    def deg(self, c: float) -> str:
        if self.src.temperature_unit() == "fahrenheit":
            return f"{round(c * 9 / 5 + 32)}°F"
        return f"{round(c)}°"


class Answer(BaseModel):
    intent: str
    text: str
    ok: bool = True                     # False: data unavailable / not understood
    sources: list[str] = []


# --------------------------------------------------------------------------- helpers

def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _weather_caveat(w: WeatherResponse, c: Ctx) -> str:
    if w.is_mock or (w.data and w.data.is_mock):
        return " This is sample data, not live weather."
    if w.status == "stale" and w.data:
        return f" Note: this is from {c.t(w.data.fetched_at)}; the weather service isn't reachable right now."
    return ""


def _no_weather(w: WeatherResponse) -> Answer:
    why = w.message or ("Set your home location first." if w.reason == "no_location" else "The weather service isn't reachable.")
    return Answer(intent="weather", ok=False, text=f"I don't have weather data right now. {why}", sources=["weather"])


def _departure(c: Ctx, status: CommuteStatus | None) -> tuple[datetime | None, str]:
    """When the user is likely to leave: today's recommended commute departure if it's
    still ahead, otherwise None (callers fall back to the travel window)."""
    if status and status.recommendation and status.state in ("active", "prefetch", "before_window"):
        dep = status.recommendation.departure_at.astimezone(c.tz)
        if dep >= c.now - timedelta(minutes=5) and dep.date() == c.now.date():
            return dep, "commute"
    return None, ""


# --------------------------------------------------------------------------- tools

async def weather_tool(c: Ctx) -> Answer:
    w = await c.src.weather()
    if not w.data:
        return _no_weather(w)
    d = w.data
    cur = d.current
    rain_today = [h.precipitation_chance_pct or 0 for h in d.hourly
                  if h.time.astimezone(c.tz).date() == c.now.date() and h.time >= c.now - timedelta(hours=1)]
    parts = [f"Right now in {d.city} it's {c.deg(cur.temperature_c)} and {cur.description.lower()}"]
    if abs(cur.feels_like_c - cur.temperature_c) >= 2:
        parts[0] += f", feeling like {c.deg(cur.feels_like_c)}"
    parts[0] += "."
    parts.append(f"Today's high is {c.deg(d.high_c)} and the low {c.deg(d.low_c)}.")
    if rain_today:
        peak = max(rain_today)
        parts.append(f"Chance of rain for the rest of today is up to {peak}%." if peak >= 20
                     else "Rain is unlikely for the rest of today.")
    if d.air_quality and d.air_quality.us_aqi >= 101:
        parts.append(f"Air quality is {d.air_quality.category.lower()} (AQI {d.air_quality.us_aqi}).")
    return Answer(intent="weather", text=" ".join(parts) + _weather_caveat(w, c), sources=["weather"])


async def rain_tool(c: Ctx) -> Answer:
    w = await c.src.weather()
    if not w.data:
        return _no_weather(w)
    dep, why = _departure(c, c.src.commute())
    window_h = c.src.travel_window_hours()
    until = dep or (c.now + timedelta(hours=window_h))
    hours = [h for h in w.data.hourly
             if c.now - timedelta(minutes=59) <= h.time <= until and h.precipitation_chance_pct is not None]
    span = f"before you leave at {c.t(dep)}" if dep else f"in the next {_plural(window_h, 'hour')}"
    sources = ["weather"] + (["commute"] if why else [])
    raining = w.data.current.precipitation_mm > 0 or w.data.current.condition.value in ("rain", "drizzle", "thunderstorm")
    if not hours:
        return Answer(intent="rain", ok=False, sources=sources,
                      text=f"I don't have an hourly rain forecast {span}." + _weather_caveat(w, c))
    peak = max(hours, key=lambda h: h.precipitation_chance_pct or 0)
    p = peak.precipitation_chance_pct or 0
    lead = "It's raining now. " if raining else ""
    if p >= 50:
        text = f"{lead}Rain is likely {span}: {p}% chance around {c.t(peak.time)}. Take an umbrella."
    elif p >= 30:
        text = f"{lead}Rain is possible {span}, up to {p}% around {c.t(peak.time)}. An umbrella wouldn't hurt."
    else:
        text = f"{lead}Rain is unlikely {span}; the highest chance is {p}%."
    return Answer(intent="rain", text=text + _weather_caveat(w, c), sources=sources)


TRAFFIC = {"light": "light traffic", "moderate": "moderate traffic", "heavy": "heavy traffic", "severe": "severe traffic"}


async def commute_tool(c: Ctx) -> Answer:
    s = c.src.commute()
    route = f"{s.home_label} to {s.office_label}"
    if s.state in ("unconfigured", "disabled"):
        return Answer(intent="commute", ok=False, sources=["commute"], text=f"I can't check the commute. {s.message}")
    r = s.recommendation
    if r is None:
        return Answer(intent="commute", ok=s.state not in ("active", "prefetch"), sources=["commute"],
                      text=f"No commute recommendation for {route} right now. {s.message}")
    basis = {"live": "live traffic", "forecast": "TomTom's traffic forecast",
             "historical": "your past trips"}.get(r.basis, r.basis)
    traffic = f" with {TRAFFIC[r.traffic_level]}" if r.traffic_level else ""
    when = "now" if r.leave_now else f"at {c.t(r.departure_at)}"
    text = (f"The best time to leave for {s.office_label} is {when}. The drive takes about {r.duration_min} minutes"
            f"{traffic}, arriving around {c.t(r.arrival_at)}, based on {basis}.")
    if s.live and not r.leave_now and s.live.duration_min is not None:
        text += f" Leaving right now would take {s.live.duration_min} minutes."
    if s.arrival_note:
        text += f" {s.arrival_note}"
    if s.state in ("after_window", "off_day", "before_window"):
        text += f" {s.message}"
    return Answer(intent="commute", text=text, sources=["commute"])


KIND_WORD = {"birthday": "birthday", "holiday": "public holiday", "observance": "observance", "event": "event"}


def _describe_day(c: Ctx, items, label: str) -> str:
    bits = []
    for it in items:
        if it.all_day:
            bits.append(it.title if it.kind == "event" else f"{it.title} ({KIND_WORD[it.kind]})")
        else:
            bits.append(f"{it.title} at {c.t(it.start)}")
    return f"{label}: {_join(bits)}."


def _wants_day(q: str, today: date) -> tuple[date, str, int]:
    if "tomorrow" in q:
        return today + timedelta(days=1), "Tomorrow", 1
    if re.search(r"\b(this|next) week\b|\bweek\b|\bupcoming\b|\bcoming up\b", q):
        return today, "This week", 7
    return today, "Today", 1


async def calendar_tool(c: Ctx) -> Answer:
    a = c.src.calendar(8)
    if a.status == "unconfigured":
        return Answer(intent="calendar", ok=False, sources=["calendar"],
                      text="No calendar is connected yet. Add one in .env on the Pi (see Settings → Calendars).")
    if a.status in ("error", "loading") and not a.days:
        return Answer(intent="calendar", ok=False, sources=["calendar"],
                      text="I couldn't reach your calendars right now, so I can't say what's on.")
    start, label, n = _wants_day(c.question, a.today)
    days = [d for d in a.days if start <= d.date < start + timedelta(days=n)]
    births = "birthday" in c.question
    if births:
        days = [d.model_copy(update={"items": [i for i in d.items if i.kind == "birthday"]}) for d in days]
        days = [d for d in days if d.items]
    caveat = " Some calendars couldn't be refreshed, so this may be incomplete." if a.status == "partial" else ""
    what = "birthdays" if births else "nothing on your calendars"
    if not days:
        return Answer(intent="calendar", sources=["calendar"],
                      text=f"{label} there's {what if not births else 'no birthday'}"
                           f"{' in the next week' if n == 7 else ''}." + caveat)
    if n == 1:
        return Answer(intent="calendar", sources=["calendar"], text=_describe_day(c, days[0].items, label) + caveat)
    lines = [_describe_day(c, d.items, d.date.strftime("%A")) for d in days[:5]]
    more = f" And {len(days) - 5} more days with items." if len(days) > 5 else ""
    return Answer(intent="calendar", sources=["calendar"], text=" ".join(lines) + more + caveat)


async def groceries_tool(c: Ctx) -> Answer:
    g = c.src.groceries()
    if g.status == "error":
        return Answer(intent="groceries", ok=False, sources=["groceries"],
                      text="I can't read the grocery list right now.")
    need = [i for i in g.items if not i.purchased]
    if not need:
        return Answer(intent="groceries", sources=["groceries"], text="The grocery list is empty; nothing to buy.")
    names = [f"{i.name} ({i.quantity})" if i.quantity else i.name for i in need]
    shown, rest = names[:12], len(names) - 12
    text = f"You need {_plural(len(need), 'item')}: {_join(shown)}"
    text += f", and {rest} more." if rest > 0 else "."
    return Answer(intent="groceries", text=text, sources=["groceries"])


async def summary_tool(c: Ctx) -> Answer:
    parts, ok, sources = [], False, []
    greeting = "Good morning" if c.now.hour < 12 else "Good afternoon" if c.now.hour < 17 else "Good evening"
    parts.append(f"{greeting}. It's {c.now.strftime('%A, %-d %B')}.")
    for tool in (weather_tool, rain_tool, commute_tool, calendar_tool, groceries_tool):
        sub = await tool(Ctx(c.src, c.tz, c.now, "today"))
        sources += sub.sources
        if sub.intent == "commute" and not sub.ok:
            continue            # no commute configured/needed: leave it out of the summary
        if sub.intent == "groceries" and "empty" in sub.text:
            continue
        if sub.intent == "rain" and "unlikely" in sub.text:
            continue            # the weather sentence already covers a dry day
        ok = ok or sub.ok
        parts.append(sub.text)
    return Answer(intent="summary", text=" ".join(parts), ok=ok, sources=sorted(set(sources)))


async def time_tool(c: Ctx) -> Answer:
    if re.search(r"\b(date|day)\b", c.question) and "time" not in c.question:
        return Answer(intent="time", text=f"It's {c.now.strftime('%A, %-d %B %Y')}.")
    return Answer(intent="time", text=f"It's {c.t(c.now)} on {c.now.strftime('%A, %-d %B')}.")


async def network_tool(c: Ctx) -> Answer:
    return Answer(intent="network", ok=False,
                  text="I can't list devices on your network yet; reliable device discovery isn't set up. "
                       "This may become available after network protection is installed.")


async def greeting_tool(c: Ctx) -> Answer:
    return Answer(intent="greeting", text="Hi, I'm listening. " + HELP)


async def help_tool(c: Ctx) -> Answer:
    return Answer(intent="help", text=HELP)


async def action_tool(c: Ctx) -> Answer:
    return Answer(intent="action", ok=False,
                  text="I can only answer questions for now; I can't add, delete or change anything. "
                       "Use the grocery list or Settings for changes.")


HELP = ("You can ask me about the weather, whether it will rain before you leave, your commute to the office, "
        "what's on your calendar today or tomorrow, what groceries you need, or ask me to summarize your day.")


@dataclass(frozen=True)
class Tool:
    name: str
    run: Callable[[Ctx], Awaitable[Answer]]
    patterns: tuple[str, ...]          # regexes on the normalized question; first tool that matches wins


# Order matters: more specific intents first.
TOOLS: tuple[Tool, ...] = (
    Tool("action", action_tool, (r"^(please )?(add|remove|delete|clear|turn|switch|set|change|disable|enable|"
                                 r"call|text|send|play|open|unlock|lock|reboot|restart|shut)\b",)),
    Tool("help", help_tool, (r"what can you do", r"\bhelp\b", r"what can i ask")),
    # Short greetings only ("Hello", "Hey there", "Are you listening?"), so real questions still route.
    Tool("greeting", greeting_tool, (r"^((hi|hello|hey|hiya|bye)( there| aura)? ?)+$", r"^good (morning|afternoon|evening)$",
                                     r"\bare you (there|listening|awake|on)\b")),
    Tool("network", network_tool, (r"\bdevices?\b.*\b(network|wifi|wi fi|connected|online)\b",
                                   r"\b(who|what)('s| is)? (is )?(connected|on (the|my) (network|wifi))")),
    Tool("summary", summary_tool, (r"\bsummar", r"\bmy day\b", r"\bbriefing\b", r"\bbrief me\b",
                                   r"what('s| is) (on )?(for )?today\b(?!.*calendar)")),
    Tool("rain", rain_tool, (r"\brain", r"\bumbrella\b", r"\bdrizzl", r"\bshower")),
    Tool("groceries", groceries_tool, (r"\bgrocer", r"\bshopping\b", r"\bbuy\b", r"\bstore\b", r"\bsupermarket\b",
                                       r"\bneed to get\b")),
    Tool("calendar", calendar_tool, (r"\bcalendar\b", r"\bevents?\b", r"\bschedule\b", r"\bbirthdays?\b",
                                     r"\bholidays?\b", r"\bappointments?\b", r"\bmeetings?\b", r"\bagenda\b",
                                     r"what('s| is) (on|happening)\b")),
    Tool("commute", commute_tool, (r"\bcommute\b", r"\btraffic\b", r"\boffice\b", r"\bwork\b", r"\bdrive\b",
                                   r"when should i (leave|go)", r"\bleave\b")),
    Tool("weather", weather_tool, (r"\bweather\b", r"\btemperature\b", r"\bforecast\b", r"\bhot\b", r"\bcold\b",
                                   r"\bhumid", r"\bsunny\b", r"\boutside\b", r"\bdegrees\b", r"\bair quality\b")),
    Tool("time", time_tool, (r"\bwhat time\b", r"\btime is it\b", r"\bwhat('s| is) the (date|day)\b",
                             r"\bwhat day is\b", r"\btoday'?s date\b")),
)

UNKNOWN = ("Sorry, I can't help with that yet. " + HELP)


def normalize(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = re.sub(r"\baura\b[,:]?\s*", "", t)               # "Aura, what's the weather"
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def route(question: str) -> Tool | None:
    q = normalize(question)
    for tool in TOOLS:
        if any(re.search(p, q) for p in tool.patterns):
            return tool
    return None


# --------------------------------------------------------------------------- conversation

class Turn(BaseModel):
    id: int
    at: datetime
    source: Literal["voice", "typed"]
    question: str
    answer: Answer
    timings_ms: dict[str, int] = Field(default_factory=dict)


class Assistant:
    """Answers questions and keeps a short in-memory transcript (never written to disk)."""

    def __init__(self, sources: Sources) -> None:
        self.src = sources
        self._turns: deque[Turn] = deque(maxlen=HISTORY)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    async def ask(self, question: str, source: Literal["voice", "typed"] = "typed",
                  timings: dict[str, int] | None = None) -> Turn:
        question = re.sub(r"\s+", " ", question).strip()[:MAX_QUESTION]
        tz = ZoneInfo(self.src.timezone())
        now = self.src.now(tz)
        tool = route(question)
        if not question:
            answer = Answer(intent="empty", ok=False, text="I didn't catch anything. Please try again.")
        elif tool is None:
            answer = Answer(intent="unknown", ok=False, text=UNKNOWN)
        else:
            try:
                answer = await tool.run(Ctx(self.src, tz, now, normalize(question)))
            except Exception:            # a broken data source must not break the assistant
                import logging
                logging.getLogger("aura.assistant").exception("assistant tool %s failed", tool.name)
                answer = Answer(intent=tool.name, ok=False,
                                text="Something went wrong while looking that up. Please try again.")
        with self._lock:
            turn = Turn(id=next(self._ids), at=now, source=source, question=question, answer=answer,
                        timings_ms=timings or {})
            self._turns.append(turn)
        return turn

    def transcript(self) -> list[Turn]:
        with self._lock:
            return list(self._turns)

    def get(self, turn_id: int) -> Turn | None:
        with self._lock:
            return next((t for t in self._turns if t.id == turn_id), None)

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()
