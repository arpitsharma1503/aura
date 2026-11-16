"""Internet speed, measured from the Pi every few minutes for the home-page pill.

Each check times three empty requests (ping: the fastest one) and one small download
(``sample_bytes``, 10 MB by default, cut off after ``MAX_SECONDS``) from Cloudflare's
public speed test. Free, no key, nothing sent but the request. It measures the Pi's own
link, so on Wi-Fi it can read lower than a wired device would.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from .storage import write_json_atomic

log = logging.getLogger(__name__)

URL = "https://speed.cloudflare.com/__down"
SOURCE = "Cloudflare speed test"
MAX_SECONDS = 8.0
FIRST_DELAY = 30          # seconds after start, so a restart doesn't slow the first page load
TRIES = 3                 # a lone timeout is a blip, not "no internet"
HISTORY = 36              # readings kept for the trend line (6 h at the default 10 min)
MANUAL_GAP = 60           # seconds: "Test now" can't start checks back to back


def quality(down: float | None, ping: int | None) -> dict | None:
    """Plain words for a reading: what it's good for."""
    if down is None or ping is None:
        return None
    if down >= 25 and ping <= 60:
        return {"level": "good", "label": "Fast", "detail": "Good for 4K video and video calls."}
    if down >= 8 and ping <= 150:
        return {"level": "ok", "label": "Okay", "detail": "Fine for HD video and browsing."}
    return {"level": "slow", "label": "Slow", "detail": "Pages and video may load slowly."}


class SpeedMonitor:
    def __init__(self, client: httpx.AsyncClient, every_minutes: int = 10, sample_bytes: int = 10_000_000,
                 now: Callable[[], datetime] | None = None, timer: Callable[[], float] = time.perf_counter,
                 retry_seconds: float = 20, history_path: Path | None = None) -> None:
        self.client = client
        self.every_minutes = every_minutes
        self.sample_bytes = sample_bytes
        self.now = now or (lambda: datetime.now().astimezone())
        self.timer = timer
        self.retry_seconds = retry_seconds
        self.down_mbps: float | None = None
        self.ping_ms: int | None = None
        self.measured_at: datetime | None = None
        self.failed_at: datetime | None = None
        self._task: asyncio.Task | None = None
        self._manual: asyncio.Task | None = None
        self.checking = False
        self.history: deque[dict] = deque(maxlen=HISTORY)
        self.history_path = history_path
        self._load_history()
        self.on_change = lambda: None

    def _load_history(self) -> None:
        """The trend line survives a restart (only times and numbers are saved)."""
        if not self.history_path:
            return
        try:
            saved = json.loads(self.history_path.read_text(encoding="utf-8"))
            self.history.extend(r for r in saved if isinstance(r, dict) and "at" in r)
        except (OSError, ValueError):
            return
        last = next((r for r in reversed(self.history) if r.get("down_mbps") is not None), None)
        if last:
            self.down_mbps, self.ping_ms = last["down_mbps"], last.get("ping_ms")
            self.measured_at = datetime.fromisoformat(last["at"])

    def _save_history(self) -> None:
        if self.history_path:
            try:
                write_json_atomic(self.history_path, list(self.history))
            except OSError:
                log.warning("Could not save speed history")

    async def _ping(self) -> int:
        best = None
        for _ in range(3):
            t0 = self.timer()
            r = await self.client.get(URL, params={"bytes": 0}, timeout=10)
            r.raise_for_status()
            ms = (self.timer() - t0) * 1000
            best = ms if best is None else min(best, ms)
        return round(best)

    async def _download(self) -> float:
        got = 0
        t0 = last = self.timer()
        async with self.client.stream("GET", URL, params={"bytes": self.sample_bytes}, timeout=MAX_SECONDS + 5) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes():
                got += len(chunk)
                last = self.timer()
                if last - t0 > MAX_SECONDS:
                    break
        elapsed = last - t0
        if not got or elapsed <= 0:
            raise ValueError("empty download")
        return round(got * 8 / elapsed / 1e6, 1)

    async def measure(self) -> None:
        if self.checking:
            return
        self.checking = True
        self.on_change()
        try:
            await self._measure()
        finally:
            self.checking = False
            self.on_change()

    def check_now(self) -> bool:
        """Start a check now ("Test now" in the Internet sheet). False if one is running or just ran."""
        last = max((t for t in (self.measured_at, self.failed_at) if t), default=None)
        busy = self.checking or (self._manual is not None and not self._manual.done())
        if busy or (last and (self.now() - last).total_seconds() < MANUAL_GAP):
            return False
        self._manual = asyncio.create_task(self.measure(), name="aura-speed-now")
        return True

    async def _measure(self) -> None:
        for attempt in range(1, TRIES + 1):
            try:
                ping = await self._ping()
                down = await self._download()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("Speed check failed (try %d of %d): %r", attempt, TRIES, e)
                if attempt < TRIES:
                    await asyncio.sleep(self.retry_seconds)
            else:
                self.ping_ms, self.down_mbps = ping, down
                self.measured_at, self.failed_at = self.now(), None
                self.history.append({"at": self.measured_at.isoformat(), "down_mbps": down, "ping_ms": ping})
                break
        else:
            self.failed_at = self.now()      # every try failed: really no internet
            self.history.append({"at": self.failed_at.isoformat(), "down_mbps": None, "ping_ms": None})
        self._save_history()

    def view(self) -> dict:
        if not self.every_minutes:
            status, message = "off", "Speed checks are turned off."
        elif self.failed_at:
            status, message = "offline", "No internet at the last check."
        elif self.measured_at:
            status, message = "ok", None
        else:
            status, message = "waiting", "First check runs shortly after start."
        return {"status": status, "message": message, "down_mbps": self.down_mbps, "ping_ms": self.ping_ms,
                "measured_at": self.measured_at.isoformat() if self.measured_at else None,
                "failed_at": self.failed_at.isoformat() if self.failed_at else None,
                "every_minutes": self.every_minutes, "source": SOURCE,
                "checking": self.checking, "quality": quality(self.down_mbps, self.ping_ms) if status == "ok" else None,
                "history": list(self.history)}

    async def run(self) -> None:
        await asyncio.sleep(FIRST_DELAY)
        while True:
            await self.measure()
            await asyncio.sleep(self.every_minutes * 60)

    def start(self) -> None:
        if self.every_minutes and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self.run(), name="aura-speed")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
