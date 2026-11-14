"""Internet speed, measured from the Pi every few minutes for the home-page pill.

Each check times three empty requests (ping: the fastest one) and one small download
(``sample_bytes``, 10 MB by default, cut off after ``MAX_SECONDS``) from Cloudflare's
public speed test. Free, no key, nothing sent but the request. It measures the Pi's own
link, so on Wi-Fi it can read lower than a wired device would.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Callable

import httpx

log = logging.getLogger(__name__)

URL = "https://speed.cloudflare.com/__down"
SOURCE = "Cloudflare speed test"
MAX_SECONDS = 8.0
FIRST_DELAY = 30          # seconds after start, so a restart doesn't slow the first page load
TRIES = 3                 # a lone timeout is a blip, not "no internet"


class SpeedMonitor:
    def __init__(self, client: httpx.AsyncClient, every_minutes: int = 10, sample_bytes: int = 10_000_000,
                 now: Callable[[], datetime] | None = None, timer: Callable[[], float] = time.perf_counter,
                 retry_seconds: float = 20) -> None:
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
        self.on_change = lambda: None

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
                break
        else:
            self.failed_at = self.now()      # every try failed: really no internet
        self.on_change()

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
                "every_minutes": self.every_minutes, "source": SOURCE}

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
