"""Gold and silver prices for India (the Price Tracker).

Source: the IBJA benchmark (India Bullion and Jewellers Association), fetched from metals.dev
(``/v1/metal/authority?authority=ibja&currency=INR&unit=g``) with the household's free API key
(``AURA_METALS_DEV_API_KEY`` in ``.env``; never sent to browsers).

* **Once a day at 12:00 India time** (the household's choice). If that fails it retries at
  12:15, 12:30 and 13:00; if the Pi was off at noon it catches up when it starts. That is
  about 30 of the free plan's 100 requests a month; a monthly cap keeps manual refreshes
  from using the rest.
* Every successful reading is stored (``metal_price`` in ``data/aura.db``), one per day.
  Changes are against the previous stored day; charts show only these real readings,
  so history starts on the day the tracker was set up.
* IBJA publishes 999 (24K) gold and 999 silver. 22K, 20K and 18K are **estimates**:
  24K x 22/24, 20/24, 18/24, labelled as such. Prices are bullion benchmarks: they exclude
  GST, making charges and a jeweller's margin.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("aura.prices")
IST = ZoneInfo("Asia/Kolkata")
URL = "https://api.metals.dev/v1/metal/authority"
FETCH_AT = time(12, 0)
RETRIES = (timedelta(minutes=15), timedelta(minutes=30), timedelta(minutes=60))
MONTHLY_CAP = 90                       # of the free plan's 100
MANUAL_GAP = timedelta(hours=3)

PURITIES = [("gold24", "24K Gold", 24, "99.9% (IBJA 999)"), ("gold22", "22K Gold", 22, "91.7%, estimated from 24K"),
            ("gold20", "20K Gold", 20, "83.3%, estimated from 24K"), ("gold18", "18K Gold", 18, "75.0%, estimated from 24K")]


class PriceError(Exception):
    pass


def parse(payload: dict) -> tuple[float, float, str]:
    """(gold 999 ₹/g, silver 999 ₹/g, timestamp) from a metals.dev IBJA response; refuses anything odd."""
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise PriceError(f"metals.dev said: {str(payload.get('error_message') or payload.get('status'))[:120]}"
                         if isinstance(payload, dict) else "Unexpected answer from metals.dev.")
    if payload.get("authority") != "ibja" or payload.get("currency") != "INR" or payload.get("unit") != "g":
        raise PriceError("metals.dev answered for a different market, currency or unit.")
    rates = payload.get("rates") or {}
    gold, silver = rates.get("ibja_gold"), rates.get("ibja_silver")
    for name, v, lo, hi in (("gold", gold, 1000, 100000), ("silver", silver, 10, 5000)):
        if not isinstance(v, (int, float)) or not lo <= v <= hi:            # ₹/g sanity bounds
            raise PriceError(f"The {name} price from metals.dev doesn't look right ({v!r}).")
    return float(gold), float(silver), str(payload.get("timestamp") or "")


def _rupees(x: Decimal) -> int:
    return int(x.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def derive(gold_g: float, silver_g: float) -> dict[str, int]:
    """Whole rupees: ₹ per 10 g for each purity, ₹ per kg for silver (half-up, as price lists round)."""
    g = Decimal(str(gold_g))
    out = {pid: _rupees(g * 10 * karat / 24) for pid, _, karat, _ in PURITIES}
    out["silver"] = _rupees(Decimal(str(silver_g)) * 1000)
    return out


class PriceTracker:
    def __init__(self, db_path: Path, api_key: str | None, client: httpx.AsyncClient,
                 now=lambda: datetime.now(IST)) -> None:
        self.key = api_key or None
        self.client = client
        self.now = now
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS metal_price (
                day TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, source_time TEXT,
                gold_g REAL NOT NULL, silver_g REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS metal_request (month TEXT PRIMARY KEY, n INTEGER NOT NULL);
        """)
        self._lock = asyncio.Lock()
        self._db_lock = threading.Lock()
        self.last_error: str | None = None
        self.last_attempt: datetime | None = None
        self._task: asyncio.Task | None = None
        self.on_change = lambda: None

    def close(self) -> None:
        self._db.close()

    # ----- storage -----
    def rows(self, days: int | None = None) -> list[dict]:
        q = "SELECT day, fetched_at, source_time, gold_g, silver_g FROM metal_price"
        args: tuple = ()
        if days:
            q += " WHERE day >= ?"
            args = ((self.now().date() - timedelta(days=days)).isoformat(),)
        rows = self._db.execute(q + " ORDER BY day", args).fetchall()
        return [{"day": d, "fetched_at": f, "source_time": s, "gold_g": g, "silver_g": sv} for d, f, s, g, sv in rows]

    def requests_this_month(self) -> int:
        row = self._db.execute("SELECT n FROM metal_request WHERE month = ?", (self.now().strftime("%Y-%m"),)).fetchone()
        return row[0] if row else 0

    def _count_request(self) -> None:
        with self._db_lock:
            self._db.execute("INSERT INTO metal_request VALUES (?, 1) ON CONFLICT(month) DO UPDATE SET n = n + 1",
                             (self.now().strftime("%Y-%m"),))

    # ----- fetching -----
    async def fetch(self, manual: bool = False) -> dict:
        if not self.key:
            raise PriceError("No metals.dev API key is set (AURA_METALS_DEV_API_KEY in .env).")
        async with self._lock:
            now = self.now()
            if self.requests_this_month() >= MONTHLY_CAP:
                raise PriceError(f"This month's {MONTHLY_CAP} requests are used up; prices resume next month.")
            if manual and self.last_attempt and now - self.last_attempt < MANUAL_GAP:
                wait = MANUAL_GAP - (now - self.last_attempt)
                raise PriceError(f"Prices were checked recently; try again in {int(wait.total_seconds() // 60) + 1} min "
                                 "(the free plan allows about 3 checks a day).")
            self.last_attempt = now
            self._count_request()
            try:
                r = await self.client.get(URL, params={"api_key": self.key, "authority": "ibja", "currency": "INR", "unit": "g"},
                                          timeout=20)
                payload = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                self.last_error = "metals.dev couldn't be reached."
                raise PriceError(self.last_error) from exc
            try:
                gold, silver, ts = parse(payload)
            except PriceError as exc:
                self.last_error = str(exc)
                raise
            with self._db_lock:
                self._db.execute("INSERT OR REPLACE INTO metal_price VALUES (?,?,?,?,?)",
                                 (now.date().isoformat(), now.isoformat(timespec="seconds"), ts, gold, silver))
            self.last_error = None
            log.info("prices: IBJA gold %.2f, silver %.2f ₹/g", gold, silver)
        self.on_change()
        return self.view()

    def due(self) -> bool:
        """Today's noon reading is due (it's past 12:00 and there's none for today)."""
        now = self.now()
        if now.time() < FETCH_AT:
            return False
        return not self._db.execute("SELECT 1 FROM metal_price WHERE day = ?", (now.date().isoformat(),)).fetchone()

    def next_run(self) -> datetime:
        now = self.now()
        noon = datetime.combine(now.date(), FETCH_AT, IST)
        if self.due():
            if self.last_attempt and self.last_attempt.date() == now.date():   # failed today: retry schedule
                tried = self.last_attempt - noon
                for gap in RETRIES:
                    if tried < gap:
                        return max(now, noon + gap)
                return noon + timedelta(days=1)
            return now
        return noon if now < noon else noon + timedelta(days=1)

    async def run(self) -> None:
        while True:
            wait = (self.next_run() - self.now()).total_seconds()
            if wait > 0:
                await asyncio.sleep(min(wait, 3600))       # re-check hourly (clock changes, long sleeps)
                continue
            try:
                await self.fetch()
            except PriceError as exc:
                log.warning("prices: %s", exc)
            except Exception:
                log.exception("prices: fetch failed")
            await asyncio.sleep(5)

    def start(self) -> None:
        if self.key and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self.run(), name="aura-prices")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ----- what screens show -----
    def view(self, days: int = 30) -> dict:
        rows = self.rows()
        now = self.now()
        status: Literal["ok", "stale", "unavailable", "not_configured"]
        if not self.key:
            status = "not_configured"
        elif not rows:
            status = "unavailable"
        else:
            latest_day = date.fromisoformat(rows[-1]["day"])
            noon_passed = now.time() >= (datetime.combine(now.date(), FETCH_AT) + RETRIES[-1]).time()
            status = "ok" if latest_day == now.date() or (latest_day == now.date() - timedelta(days=1) and not noon_passed) else "stale"
        items = []
        if rows:
            cur = derive(rows[-1]["gold_g"], rows[-1]["silver_g"])
            prev = derive(rows[-2]["gold_g"], rows[-2]["silver_g"]) if len(rows) > 1 else None
            for pid, label, karat, note in PURITIES + [("silver", "Silver", None, "99.9% (IBJA 999)")]:
                change = cur[pid] - prev[pid] if prev else None
                items.append({"id": pid, "label": label, "unit": "kg" if pid == "silver" else "10 g", "price": cur[pid],
                              "change": change, "change_pct": round(change / prev[pid] * 100, 2) if prev and prev[pid] else None,
                              "previous_day": rows[-2]["day"] if prev else None, "estimate": karat not in (None, 24), "note": note})
        history = [{"day": r["day"], **derive(r["gold_g"], r["silver_g"])} for r in rows[-400:]]
        return {
            "status": status, "message": {"not_configured": "Add a metals.dev API key to show prices.",
                                          "unavailable": self.last_error or "No prices yet: the first one arrives at 12:00.",
                                          "stale": f"Showing the last price from {rows[-1]['day'] if rows else ''}; "
                                                   + (self.last_error or "today's update hasn't arrived."),
                                          "ok": None}[status],
            "source": "IBJA (India Bullion and Jewellers Association) via metals.dev",
            "basis": "Bullion benchmark for India, before GST (3%), making charges and the jeweller's margin.",
            "day": rows[-1]["day"] if rows else None,
            "fetched_at": rows[-1]["fetched_at"] if rows else None,
            "source_time": rows[-1]["source_time"] if rows else None,
            "items": items, "history": history,
            "schedule": "Once a day at 12:00 India time",
            "next_update": self.next_run().isoformat(timespec="minutes") if self.key else None,
            "requests_this_month": self.requests_this_month(), "monthly_cap": MONTHLY_CAP,
        }
