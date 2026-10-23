"""Wake word ("Hey Aura") for the voice worker. Off unless Settings → Assistant enables it.

While enabled, one continuous microphone stream (arecord, fixed arguments) feeds:

1. Vosk keyword spotting, with a grammar that only knows the wake phrase, so normal
   conversation is never transcribed. A candidate needs both words with high confidence.
2. A second check: Whisper (already loaded for questions) transcribes the candidate
   utterance and must hear "Aura" right after the greeting. Lookalikes such as
   "Hey, are you ready?" or "Hey Laura" are dropped here without any sign on screen.
3. If the utterance already contains the question ("Hey Aura, what's the weather?"),
   that question is answered directly; after just "Hey Aura" the stream switches to
   capturing the follow-up question.

Button presses use the same stream (``capture``), so the two never fight over the
microphone. Audio lives only in a short in-memory ring buffer (about 12 s) and is never
written anywhere; candidate texts that turn out not to be the wake word are not logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path

from .audio import FRAME, RATE, Endpointer, rms_of

log = logging.getLogger("aura.voice.wake")

CHUNK_FRAMES = 3                              # 3 x 30 ms = 90 ms per read
CHUNK_BYTES = FRAME * 2 * CHUNK_FRAMES
RING_SECONDS = 12
PRE_ROLL = 0.35                               # keep a little audio before the wake word
MIN_CONF = 0.8
FILLERS = {"uh", "um", "er", "oh", "so", "okay", "ok", "well", "hmm", "and"}
WAKE_RE = re.compile(
    r"^(?P<pre>.*?)\b(?:(?:hey|hi|hay|a|okay|ok)\W+)?(?:aura|aora|ora|aurora|aara|orah|oura|aurah|auro|biora|ayora|aiora|ayura|heyora|hiora)\b[\s,.!?:;-]*",
    re.IGNORECASE)


def split_wake(text: str) -> str | None:
    """The question after the wake phrase ('' if none), or None if there is no wake phrase.
    The wake phrase must come first; only filler words like "uh" or "so" may precede it."""
    m = WAKE_RE.search(text or "")
    if not m:
        return None
    lead = [w.strip(",.!?;:'\"-").lower() for w in m.group("pre").split()]
    if any(w and w not in FILLERS for w in lead):      # "Her aura was ..." is not a wake phrase
        return None
    return text[m.end():].strip()


class Capture:
    """A question being recorded from the shared stream (Endpointer decides when it ends)."""

    def __init__(self, max_seconds: int, prefill: list[bytes] | None = None) -> None:
        self.ep = Endpointer()
        self.limit = max_seconds * RATE * 2
        self.chunks: list[bytes] = []
        self.done: asyncio.Future = asyncio.get_running_loop().create_future()
        self.reason = "max"
        self.stop_requested = False
        self.cancelled = False
        for c in prefill or []:
            self.feed(c)

    @property
    def size(self) -> int:
        return sum(len(c) for c in self.chunks)

    def feed(self, chunk: bytes) -> None:
        if self.done.done():
            return
        self.chunks.append(chunk)
        for i in range(0, len(chunk) - FRAME * 2 + 1, FRAME * 2):
            state = self.ep.feed(rms_of(chunk[i:i + FRAME * 2]))
            if state != "continue":
                self.reason = state
                return self.finish()
        if self.stop_requested:
            self.reason = "cancelled" if self.cancelled else "stopped"
            return self.finish()
        if self.size >= self.limit:
            self.reason = "max"
            self.finish()

    def finish(self) -> None:
        if not self.done.done():
            self.done.set_result((b"".join(self.chunks), self.ep, self.reason))


class WakeListener:
    def __init__(self, device: str, model_dir: Path, transcribe: Callable[..., Awaitable[str]],
                 notify: Callable[[dict], Awaitable[None]], phrase: str = "hey aura") -> None:
        self.device, self.model_dir, self.phrase = device, model_dir, phrase.lower()
        self.transcribe, self.notify = transcribe, notify
        self.enabled = False
        self.running = False
        self.error: str | None = None
        self.max_seconds = 10
        self._model = None
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._capture: Capture | None = None
        self._busy = False                      # verifying a candidate / handling a wake
        self._ring: deque = deque()
        self.candidates = 0
        self.wakes = 0

    # ----- control -----

    def status(self) -> dict:
        return {"enabled": self.enabled, "running": self.running, "phrase": self.phrase, "error": self.error,
                "model": (self.model_dir / "README").exists()}

    async def set_enabled(self, enabled: bool, max_seconds: int = 10) -> None:
        self.max_seconds = max_seconds
        if enabled == self.enabled and (not enabled or (self._task and not self._task.done())):
            return
        self.enabled = enabled
        if enabled:
            self._task = asyncio.create_task(self._run())
        else:
            await self._stop()

    async def _stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task, self._proc, self.running = None, None, False
        if self._capture:
            self._capture.finish()

    # ----- shared-stream capture (used by button presses while the wake word is on) -----

    @property
    def capturing(self) -> bool:
        return self._capture is not None and not self._capture.done.done()

    async def capture(self, max_seconds: int, prefill: list[bytes] | None = None):
        if self.capturing:
            raise RuntimeError("busy")
        cap = Capture(max_seconds, prefill)
        self._capture = cap
        try:
            return await asyncio.wait_for(cap.done, timeout=max_seconds + 5)
        finally:
            if self._capture is cap:
                self._capture = None

    def stop_capture(self, cancel: bool = False) -> None:
        if self._capture:
            self._capture.stop_requested = True
            self._capture.cancelled = cancel

    # ----- main loop -----

    def _load(self):
        if self._model is None:
            from vosk import Model, SetLogLevel
            SetLogLevel(-1)
            self._model = Model(str(self.model_dir))
        return self._model

    def _recognizer(self):
        from vosk import KaldiRecognizer
        rec = KaldiRecognizer(self._model, RATE, json.dumps([self.phrase, "[unk]"]))
        rec.SetWords(True)
        return rec

    async def _run(self) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(None, self._load)
        except Exception as exc:                  # vosk missing or model not downloaded
            self.error = f"wake-word model unavailable: {type(exc).__name__}"
            log.warning("%s", self.error)
            self.enabled = False
            return
        while self.enabled:
            try:
                await self._listen()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("wake listener stopped")
            self.running = False
            if self._capture:
                self._capture.finish()
            if self.enabled:
                await asyncio.sleep(5)            # mic unplugged or busy: try again shortly

    async def _listen(self) -> None:
        argv = ["arecord", "-D", self.device, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw", "-q", "-"]
        self._proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                          stderr=asyncio.subprocess.PIPE)
        ring: deque[tuple[int, bytes]] = deque(maxlen=int(RING_SECONDS * RATE * 2 / CHUNK_BYTES))
        self._ring = ring
        pos = 0                                   # samples since the stream started
        rec, rec_start = self._recognizer(), 0
        try:
            while self.enabled:
                try:
                    chunk = await asyncio.wait_for(self._proc.stdout.readexactly(CHUNK_BYTES), timeout=3)
                except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                    err = (await self._proc.stderr.read()).decode(errors="replace").strip() if self._proc.stderr else ""
                    self.error = "The microphone isn't available" + (f": {err.splitlines()[-1]}" if err else ".")
                    return
                if not self.running:
                    self.running, self.error = True, None
                ring.append((pos, chunk))
                pos += len(chunk) // 2
                if self._capture and not self._capture.done.done():
                    self._capture.feed(chunk)
                    rec, rec_start = None, pos         # restart spotting after the capture
                    continue
                if self._busy:
                    continue
                if rec is None:
                    rec = self._recognizer()
                if rec.AcceptWaveform(chunk):
                    start = self._candidate(json.loads(rec.Result()))
                    if start is not None:
                        self.candidates += 1
                        first = rec_start + int(max(0.0, start - PRE_ROLL) * RATE)
                        clip = b"".join(c for p, c in ring if p + len(c) // 2 > first)
                        self._busy = True
                        asyncio.create_task(self._verify(clip, pos))
                        rec, rec_start = None, pos
        finally:
            if self._proc.returncode is None:
                self._proc.terminate()
            await self._proc.wait()

    def _candidate(self, result: dict) -> float | None:
        """Start time (s) of a confident "hey aura" in a final Vosk result, else None."""
        words = result.get("result") or []
        first, second = self.phrase.split()[0], self.phrase.split()[-1]
        for a, b in zip(words, words[1:]):
            if a["word"] == first and b["word"] == second and min(a["conf"], b["conf"]) >= MIN_CONF \
                    and b["start"] - a["end"] < 0.6:
                return a["start"]
        return None

    async def _verify(self, clip: bytes, after: int) -> None:
        try:
            text = await self.transcribe(clip, False, True)
            question = split_wake(text)
            if question is None:
                log.info("wake candidate rejected by the second check")      # never log what was said
                return
            self.wakes += 1
            if question:
                await self.notify({"event": "question", "text": question})
                return
            # Just "Hey Aura": listen for the question on the same stream.
            await self.notify({"event": "listening", "max_seconds": self.max_seconds})
            # Audio that arrived while we were checking may already hold the start of the question.
            prefill = [c for p, c in list(self._ring) if p >= after]
            pcm, ep, reason = await self.capture(self.max_seconds, prefill)
            if not ep.heard_speech and not ep.maybe_speech:
                await self.notify({"event": "no_speech", "levels": ep.levels()})
                return
            await self.notify({"event": "processing"})
            question = (await self.transcribe(pcm, not ep.heard_speech)).strip()
            if question and split_wake(question) is not None:   # "Hey Aura" again + question
                question = split_wake(question) or ""
            await self.notify({"event": "question", "text": question} if question else
                              {"event": "no_speech", "levels": ep.levels()})
        except Exception:
            log.exception("wake handling failed")
            try:
                await self.notify({"event": "error"})
            except Exception:
                pass
        finally:
            self._busy = False
