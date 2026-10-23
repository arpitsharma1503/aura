"""Dashboard side of the voice assistant: talks to the local voice worker
(app/voice_worker.py on 127.0.0.1) and runs one push-to-talk interaction:

    listening -> processing -> answered | error

Each state change is published as an "assistant" Server-Sent Event so every
open screen shows whether the microphone is on. Audio never reaches this
process; only the transcript does.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Literal

import httpx

from .assistant import Assistant, Turn
from .wake import split_wake

log = logging.getLogger("aura.voice")

State = Literal["idle", "checking", "listening", "processing", "answered", "error"]


class VoiceError(Exception):
    def __init__(self, message: str, status: int = 503, retry: bool = True) -> None:
        super().__init__(message)
        self.status, self.retry = status, retry


class VoiceClient:
    """HTTP client for the voice worker. Never forwards anything to the internet."""

    def __init__(self, port: int, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.client = httpx.AsyncClient(base_url=self.base, timeout=httpx.Timeout(30, connect=2), transport=transport)

    async def _call(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = await self.client.request(method, path, **kw)
        except httpx.ConnectError as exc:
            raise VoiceError("The voice service isn't running. Check: systemctl --user status aura-voice") from exc
        except httpx.TimeoutException as exc:
            raise VoiceError("The voice service took too long to respond.") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = None
            raise VoiceError(detail or f"Voice service error ({r.status_code}).", status=503 if r.status_code >= 500 else r.status_code)
        return r

    async def health(self) -> dict | None:
        try:
            return (await self._call("GET", "/health")).json()
        except VoiceError:
            return None

    async def record(self, max_seconds: int) -> dict:
        return (await self._call("POST", "/record", json={"max_seconds": max_seconds},
                                 timeout=httpx.Timeout(max_seconds + 10, connect=2))).json()

    async def stop(self) -> None:
        await self._call("POST", "/stop")

    async def cancel(self) -> None:
        await self._call("POST", "/cancel")

    async def transcribe(self, clip_id: str) -> dict:
        return (await self._call("POST", f"/transcribe/{clip_id}", timeout=httpx.Timeout(60, connect=2))).json()

    async def configure_wake(self, enabled: bool, max_seconds: int, notify_url: str) -> dict:
        return (await self._call("POST", "/wake", json={"enabled": enabled, "max_seconds": max_seconds,
                                                        "notify_url": notify_url})).json()

    async def speak(self, text: str) -> bytes:
        return (await self._call("POST", "/speak", json={"text": text[:1200]})).content

    async def aclose(self) -> None:
        await self.client.aclose()


class VoiceSession:
    """One push-to-talk interaction at a time, with its state broadcast."""

    def __init__(self, voice: VoiceClient, assistant: Assistant, publish: Callable[[str, dict], None]) -> None:
        self.voice, self.assistant, self.publish = voice, assistant, publish
        self.state: State = "idle"
        self.error: str | None = None
        self._lock = asyncio.Lock()

    def _set(self, state: State, **extra) -> None:
        self.state = state
        self.error = extra.get("error")
        self.publish("assistant", {"state": state, **extra})

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def listen(self, max_seconds: int) -> Turn | None:
        if self._lock.locked():
            raise VoiceError("Already listening.", status=409, retry=False)
        async with self._lock:
            t0 = time.monotonic()
            self._set("listening", max_seconds=max_seconds)
            try:
                rec = await self.voice.record(max_seconds)
                lv = rec.get("levels") or {}
                log.info("listen: %s after %.1fs (room %s dBFS, loudest %s dBFS, speech above %s dBFS%s)",
                         rec.get("reason"), rec.get("seconds") or 0, lv.get("floor_dbfs"), lv.get("peak_dbfs"),
                         lv.get("threshold_dbfs"), ", quiet: checked by Whisper" if rec.get("uncertain") else "")
                if not rec.get("clip_id"):
                    if rec.get("reason") == "cancelled":
                        self._set("idle")
                        return None
                    raise VoiceError("I didn't hear anything. Speak within about a metre of the Pi's microphone and try again.",
                                     status=422)
                self._set("processing")
                t1 = time.monotonic()
                result = await self.voice.transcribe(rec["clip_id"])
                text = (result.get("text") or "").strip()
                rest = split_wake(text)           # "Hey Aura, ..." said after pressing the button
                if rest is not None:
                    text = rest or "Hey Aura"     # only the wake phrase: answer with the greeting
                if not text:
                    raise VoiceError("I couldn't make out any words. Please try again.", status=422)
                turn = await self.assistant.ask(text, source="voice", timings={
                    "record": int((t1 - t0) * 1000), "transcribe": int(result.get("stt_ms", 0))})
            except VoiceError as exc:
                self._set("error", error=str(exc), retry=exc.retry)
                raise
            except Exception as exc:
                log.exception("voice interaction failed")
                self._set("error", error="Something went wrong. Please try again.", retry=True)
                raise VoiceError("Something went wrong. Please try again.") from exc
            self._set("answered", turn_id=turn.id)
            return turn

    async def wake_event(self, ev: dict) -> None:
        """Events from the wake-word listener ("Hey Aura") in the voice worker."""
        kind = ev.get("event")
        if self._lock.locked():                 # a button-started question is in progress
            return
        if kind == "checking":
            self._set("checking", wake=True)
        elif kind == "idle":
            if self.state == "checking":
                self._set("idle", wake=True)
        elif kind == "listening":
            log.info("wake word heard")
            self._set("listening", max_seconds=ev.get("max_seconds"), wake=True)
        elif kind == "processing":
            self._set("processing", wake=True)
        elif kind == "question":
            text = (ev.get("text") or "").strip()[:300]
            if not text:
                self._set("error", error="I couldn't make out a question. Say “Hey Aura” and try again.", retry=False, wake=True)
                return
            log.info("wake word heard with a question")
            self._set("processing", wake=True)
            turn = await self.assistant.ask(text, source="voice")
            self._set("answered", turn_id=turn.id, wake=True)
        elif kind == "no_speech":
            self._set("error", error="I didn't hear a question after “Hey Aura”.", retry=False, wake=True)
        else:
            self._set("error", error="Something went wrong with the wake word. Please try again.", retry=False, wake=True)

    async def stop(self) -> None:
        if self.busy:
            await self.voice.stop()

    async def cancel(self) -> None:
        if self.busy:
            await self.voice.cancel()
