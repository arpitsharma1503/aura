"""Gold and silver prices for India (the Price Tracker).

Source: the IBJA benchmark (India Bullion and Jewellers Association), fetched from metals.dev
(``/v1/metal/authority?authority=ibja&currency=INR&unit=g``) with the household's free API key
(``AURA_METALS_DEV_API_KEY`` in ``.env``; never sent to browsers).

* **Once a day at 12:00 India time** (the household's choice): no manual refresh. If that
  check fails it tries again (12:15, 12:30, 13:00); if it returns yesterday's price unchanged
  (IBJA's new rate often reaches metals.dev after 12:00) it checks again at 12:45 and 13:30,
  then accepts it (holidays have no new rate). If the Pi was off at noon it checks once when
  it starts. About 30-70 of the free plan's 100 requests a month; a monthly cap is a safety net.
* Every successful reading is stored (``metal_price`` in ``data/aura.db``), one per day.
  Changes are against the previous stored day; charts show only these real readings,
  so history starts on the day the tracker was set up.
* Shown: 24K gold (IBJA 999) per 10 g and silver (IBJA 999) per kg, as published. Bullion
  benchmarks: they exclude GST, making charges and a jeweller's margin.
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
RECHECKS = (time(12, 45), time(13, 30))   # when the noon price is still yesterday's
SAME_PRICE = 0.0002                       # within 0.02%: the same IBJA rate
MONTHLY_CAP = 90                       # of the free plan's 100; up to 3 checks a day
ITEMS = [("gold24", "24K Gold", "10 g"), ("silver", "Silver", "kg")]
# Lower purities are calculated from the 24K price (not fetched): 22K uses the 916 hallmark, 20K and 18K
# the household's factors.
# 14K wasn't given a factor: it uses 14/24, the same rule as 20K (20/24) and 18K (18/24).
PURITY_FACTORS = [("gold22", "22K", "0.916"), ("gold20", "20K", "0.8333"), ("gold18", "18K", "0.75"), ("gold14", "14K", "0.5833")]


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
    """Whole rupees (half-up, as price lists round): 24K gold per 10 g, silver per kg."""
    return {"gold24": _rupees(Decimal(str(gold_g)) * 10), "silver": _rupees(Decimal(str(silver_g)) * 1000)}


def purities(gold24_per_10g: int) -> dict[str, int]:
    """22K/20K/18K/14K per 10 g = the (rounded) 24K price x the purity factor, to the nearest rupee."""
    return {pid: _rupees(Decimal(gold24_per_10g) * Decimal(f)) for pid, _, f in PURITY_FACTORS}


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
    async def fetch(self) -> dict:
        if not self.key:
            raise PriceError("No metals.dev API key is set (AURA_METALS_DEV_API_KEY in .env).")
        async with self._lock:
            now = self.now()
            if self.requests_this_month() >= MONTHLY_CAP:
                raise PriceError(f"This month's {MONTHLY_CAP} requests are used up; prices resume next month.")
            if self._db.execute("SELECT 1 FROM metal_price WHERE day = ?", (now.date().isoformat(),)).fetchone() \
                    and not self._recheck_due():
                raise PriceError("Today's price is already in: one check a day.")
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

    def _next_recheck(self) -> datetime | None:
        """When today's price is still yesterday's: the next recheck after it was read (or None)."""
        now = self.now()
        rows = self._db.execute("SELECT day, fetched_at, gold_g, silver_g FROM metal_price ORDER BY day DESC LIMIT 2").fetchall()
        if len(rows) < 2 or rows[0][0] != now.date().isoformat():
            return None
        # "Unchanged" allows metals.dev's conversion jitter (seen: 13758.0178 then 13758.0192).
        if any(abs(a - b) > b * SAME_PRICE for a, b in zip(rows[0][2:], rows[1][2:])):
            return None
        fetched = datetime.fromisoformat(rows[0][1])
        return next((at for at in (datetime.combine(now.date(), t, IST) for t in RECHECKS) if at > fetched), None)

    def _recheck_due(self) -> bool:
        at = self._next_recheck()
        return at is not None and self.now() >= at

    def due(self) -> bool:
        """Today's noon reading is due (past 12:00 and none for today), or a recheck is."""
        now = self.now()
        if now.time() < FETCH_AT:
            return False
        if self._db.execute("SELECT 1 FROM metal_price WHERE day = ?", (now.date().isoformat(),)).fetchone():
            return self._recheck_due()
        return True

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
        if now >= noon and (at := self._next_recheck()):
            return at
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
        missed = None
        if rows:
            cur = derive(rows[-1]["gold_g"], rows[-1]["silver_g"])
            # Day-on-day means yesterday: after a missed day there is no comparison, never an older day.
            yesterday = (date.fromisoformat(rows[-1]["day"]) - timedelta(days=1)).isoformat()
            prev_row = rows[-2] if len(rows) > 1 and rows[-2]["day"] == yesterday else None
            missed = yesterday if len(rows) > 1 and not prev_row else None
            prev = derive(prev_row["gold_g"], prev_row["silver_g"]) if prev_row else None
            for pid, label, unit in ITEMS:
                change = cur[pid] - prev[pid] if prev else None
                items.append({"id": pid, "label": label, "unit": unit, "price": cur[pid],
                              "previous": prev[pid] if prev else None, "change": change,
                              "change_pct": round(change / prev[pid] * 100, 2) if prev and prev[pid] else None,
                              "previous_day": yesterday if prev else None, "missed_day": missed})
        history = [{"day": r["day"], **derive(r["gold_g"], r["silver_g"])} for r in rows[-400:]]
        purity = []
        if rows:
            cur_p = purities(cur["gold24"])
            prev_p = purities(prev["gold24"]) if prev else None
            for pid, karat, f in PURITY_FACTORS:
                ch = cur_p[pid] - prev_p[pid] if prev_p else None
                purity.append({"id": pid, "karat": karat, "factor": f, "unit": "10 g", "price": cur_p[pid],
                               "previous": prev_p[pid] if prev_p else None, "change": ch,
                               "change_pct": round(ch / prev_p[pid] * 100, 2) if prev_p and prev_p[pid] else None,
                               "missed_day": missed})
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
            "gold_purities": purity,              # calculated from 24K (PURITY_FACTORS), not quoted
            "schedule": "Once a day at 12:00 India time",
            "next_update": self.next_run().isoformat(timespec="minutes") if self.key else None,
            "requests_this_month": self.requests_this_month(), "monthly_cap": MONTHLY_CAP,
        }
