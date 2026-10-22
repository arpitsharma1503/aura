"""AURA voice worker: microphone, speech-to-text and text-to-speech.

Runs as its own process (``python -m app.voice_worker``, systemd user unit
``aura-voice``) so the models' memory use or a crash never affects the
dashboard. It listens on 127.0.0.1 only; the dashboard talks to it.

Privacy:
- Records only when asked (push-to-talk), for at most ``MAX_SECONDS``.
- Audio is held in memory, transcribed, then dropped. Nothing is written to disk.
- Recording uses fixed arguments; nothing from a request reaches a shell.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import io
import logging
import math
import secrets
import time
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .security import is_loopback

log = logging.getLogger("aura.voice")

RATE = 16_000
FRAME = 480                      # 30 ms of 16-bit mono
MAX_SECONDS = 15
CLIP_TTL = 120                   # seconds an untranscribed clip may stay in memory
PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


# --------------------------------------------------------------------------- end of speech detection

@dataclass
class Endpointer:
    """Simple energy-based end-of-speech detection for push-to-talk.

    Learns the room's noise floor from the first ~0.3 s, treats frames well above
    it as speech, and stops after ``silence_s`` of quiet once speech has started,
    or after ``no_speech_s`` if nobody speaks at all.
    """
    silence_s: float = 1.2
    no_speech_s: float = 5.0
    min_speech_s: float = 0.25
    floor: float | None = None
    frames: int = 0
    speech_frames: int = 0
    quiet_frames: int = 0
    calib: list[float] = field(default_factory=list)

    def threshold(self) -> float:
        return max((self.floor or 100) * 3.0, 350)

    def feed(self, rms: float) -> Literal["continue", "done", "no_speech"]:
        self.frames += 1
        if self.floor is None:
            self.calib.append(rms)
            if len(self.calib) >= 10:
                self.floor = sorted(self.calib)[len(self.calib) // 2]
            return "continue"
        if rms > self.threshold():
            self.speech_frames += 1
            self.quiet_frames = 0
        elif self.speech_frames:
            self.quiet_frames += 1
            # slowly track the floor while quiet
            self.floor = 0.95 * self.floor + 0.05 * rms
        sec = FRAME / RATE
        if self.speech_frames * sec >= self.min_speech_s and self.quiet_frames * sec >= self.silence_s:
            return "done"
        if self.speech_frames * sec < self.min_speech_s and self.frames * sec >= self.no_speech_s:
            return "no_speech"
        return "continue"

    @property
    def heard_speech(self) -> bool:
        return self.speech_frames * FRAME / RATE >= self.min_speech_s


def rms_of(chunk: bytes) -> float:
    a = array.array("h", chunk)
    if not a:
        return 0.0
    return math.sqrt(sum(x * x for x in a) / len(a))


def dbfs(rms: float) -> float:
    return round(20 * math.log10(max(rms, 1) / 32768), 1)


# --------------------------------------------------------------------------- engines

class SpeechToText:
    def __init__(self, model: str, models_dir: Path, threads: int) -> None:
        self.name, self.dir, self.threads = model, models_dir / "whisper", threads
        self.state: Literal["loading", "ready", "missing", "error"] = "loading"
        self.error: str | None = None
        self._model = None

    def load(self, allow_download: bool = False) -> None:
        try:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.name, device="cpu", compute_type="int8", cpu_threads=self.threads,
                                       download_root=str(self.dir), local_files_only=not allow_download)
            self.state, self.error = "ready", None
        except ImportError:
            self.state, self.error = "error", "faster-whisper is not installed in the venv"
        except Exception as exc:      # model files missing / corrupt
            missing = "local" in str(exc).lower() or "not found" in str(exc).lower() or "snapshot" in str(exc).lower()
            self.state = "missing" if missing else "error"
            self.error = ("speech model not downloaded (run: .venv/bin/python -m app.voice_worker --download)"
                          if missing else f"speech model failed to load: {type(exc).__name__}")
            log.warning("STT %s: %s", self.state, exc)

    def transcribe(self, pcm: bytes) -> str:
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self._model.transcribe(
            audio, language="en", beam_size=1, vad_filter=False, condition_on_previous_text=False,
            initial_prompt="Aura, weather, rain, umbrella, commute, office, traffic, calendar, birthday, groceries.")
        return " ".join(s.text.strip() for s in segments).strip()


class TextToSpeech:
    def __init__(self, voice: str, models_dir: Path) -> None:
        self.name, self.dir = voice, models_dir / "piper"
        self.state: Literal["loading", "ready", "missing", "error"] = "loading"
        self.error: str | None = None
        self._voice = None

    @property
    def model_path(self) -> Path:
        return self.dir / f"{self.name}.onnx"

    def load(self) -> None:
        if not self.model_path.exists() or not Path(f"{self.model_path}.json").exists():
            self.state = "missing"
            self.error = "voice not downloaded (run: .venv/bin/python -m app.voice_worker --download)"
            return
        try:
            from piper import PiperVoice
            self._voice = PiperVoice.load(str(self.model_path))
            self.state, self.error = "ready", None
        except ImportError:
            self.state, self.error = "error", "piper-tts is not installed in the venv"
        except Exception as exc:
            self.state, self.error = "error", f"voice failed to load: {type(exc).__name__}"
            log.warning("TTS error: %s", exc)

    def synthesize(self, text: str) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            if hasattr(self._voice, "synthesize_wav"):       # piper-tts >= 1.3
                self._voice.synthesize_wav(text, wf)
            else:                                              # older API
                self._voice.synthesize(text, wf)
        return buf.getvalue()


# --------------------------------------------------------------------------- microphone

def mic_present(device: str) -> bool:
    """True when the named ALSA capture card exists (no audio is opened)."""
    card = device.split("CARD=")[1].split(",")[0] if "CARD=" in device else None
    if card is None:
        return True       # e.g. "default": can't check without opening it
    try:
        return any(line.split("[", 1)[1].split("]")[0].strip() == card
                   for line in Path("/proc/asound/cards").read_text().splitlines() if "[" in line)
    except OSError:
        return False


@dataclass
class Clip:
    pcm: bytes
    created: float
    seconds: float


class Recorder:
    def __init__(self, device: str) -> None:
        self.device = device
        self.proc: asyncio.subprocess.Process | None = None
        self._stop = asyncio.Event()
        self._cancel = False
        self.level = -90.0

    @property
    def busy(self) -> bool:
        return self.proc is not None

    def argv(self, max_seconds: int) -> list[str]:
        return ["arecord", "-D", self.device, "-f", "S16_LE", "-r", str(RATE), "-c", "1",
                "-t", "raw", "-q", "-d", str(max_seconds), "-"]

    async def record(self, max_seconds: int) -> tuple[bytes, Endpointer, str]:
        if self.busy:
            raise HTTPException(409, "Already listening.")
        self._stop.clear()
        self._cancel = False
        ep = Endpointer()
        chunks: list[bytes] = []
        reason = "max"
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv(max_seconds), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            while True:
                if self._stop.is_set():
                    reason = "stopped"
                    break
                try:
                    chunk = await asyncio.wait_for(self.proc.stdout.readexactly(FRAME * 2), timeout=2)
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        chunks.append(exc.partial)
                    break
                except asyncio.TimeoutError:
                    break
                chunks.append(chunk)
                r = rms_of(chunk)
                self.level = dbfs(r)
                state = ep.feed(r)
                if state != "continue":
                    reason = state
                    break
        finally:
            if self.proc.returncode is None:
                self.proc.terminate()
            _, err = await self.proc.communicate()
            code = self.proc.returncode
            self.proc = None
        if not chunks:
            msg = (err or b"").decode(errors="replace").strip().splitlines()
            detail = msg[-1] if msg else f"arecord exited with {code}"
            if "busy" in detail.lower():
                detail = "The microphone is in use by another program."
            elif "no such" in detail.lower() or "unknown" in detail.lower():
                detail = "The microphone isn't connected."
            raise HTTPException(503, f"Couldn't record: {detail}")
        if self._cancel:
            reason = "cancelled"
        return b"".join(chunks), ep, reason

    def stop(self) -> None:
        self._stop.set()

    def cancel(self) -> None:
        self._cancel = True
        self._stop.set()


# --------------------------------------------------------------------------- HTTP API (loopback only)

class RecordIn(BaseModel):
    max_seconds: int = Field(default=10, ge=2, le=MAX_SECONDS)


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=1200)


def create_worker(settings: Settings, load_models: bool = True) -> FastAPI:
    models_dir = settings.data_dir / "models"
    stt = SpeechToText(settings.voice_stt_model, models_dir, settings.voice_threads)
    tts = TextToSpeech(settings.voice_tts_voice, models_dir)
    rec = Recorder(settings.voice_mic_device)
    clips: dict[str, Clip] = {}
    busy = asyncio.Lock()             # one transcription / synthesis at a time (CPU bound)

    def sweep() -> None:
        now = time.monotonic()
        for k in [k for k, c in clips.items() if now - c.created > CLIP_TTL]:
            del clips[k]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if load_models:      # load in the background so /health answers ("loading") at once
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, stt.load)
            loop.run_in_executor(None, tts.load)
        yield

    app = FastAPI(title="AURA voice", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.stt, app.state.tts, app.state.rec, app.state.clips = stt, tts, rec, clips

    @app.middleware("http")
    async def loopback_only(request: Request, call_next):  # type: ignore[no-untyped-def]
        host = request.client.host if request.client else None
        if not is_loopback(host):
            return Response(status_code=403)
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict:
        sweep()
        return {
            "stt": {"state": stt.state, "model": stt.name, "error": stt.error},
            "tts": {"state": tts.state, "voice": tts.name, "error": tts.error},
            "mic": {"present": mic_present(rec.device), "device": rec.device, "busy": rec.busy},
            "clips_in_memory": len(clips),
        }

    @app.get("/level")
    async def level() -> dict:
        return {"listening": rec.busy, "dbfs": rec.level}

    @app.post("/record")
    async def record(body: RecordIn) -> dict:
        if not mic_present(rec.device):
            raise HTTPException(503, "The microphone isn't connected.")
        t0 = time.monotonic()
        pcm, ep, reason = await rec.record(body.max_seconds)
        secs = round(len(pcm) / 2 / RATE, 2)
        if reason == "cancelled":
            return {"clip_id": None, "reason": reason, "seconds": secs}
        if not ep.heard_speech and reason in ("no_speech", "max", "stopped"):
            return {"clip_id": None, "reason": "no_speech", "seconds": secs}
        sweep()
        cid = secrets.token_urlsafe(9)
        clips[cid] = Clip(pcm, time.monotonic(), secs)
        return {"clip_id": cid, "reason": reason, "seconds": secs, "record_ms": int((time.monotonic() - t0) * 1000)}

    @app.post("/stop")
    async def stop() -> dict:
        rec.stop()
        return {"stopping": rec.busy}

    @app.post("/cancel")
    async def cancel() -> dict:
        rec.cancel()
        return {"cancelled": rec.busy}

    @app.post("/transcribe/{clip_id}")
    async def transcribe(clip_id: str) -> dict:
        clip = clips.pop(clip_id, None)          # dropped from memory whatever happens next
        if clip is None:
            raise HTTPException(404, "That recording has expired.")
        if stt.state != "ready":
            raise HTTPException(503, f"Speech recognition isn't ready: {stt.error or stt.state}.")
        t0 = time.monotonic()
        async with busy:
            text = await asyncio.get_running_loop().run_in_executor(None, stt.transcribe, clip.pcm)
        del clip
        return {"text": text, "stt_ms": int((time.monotonic() - t0) * 1000)}

    @app.post("/speak")
    async def speak(body: SpeakIn) -> Response:
        if tts.state != "ready":
            raise HTTPException(503, f"Spoken replies aren't available: {tts.error or tts.state}.")
        t0 = time.monotonic()
        async with busy:
            wav = await asyncio.get_running_loop().run_in_executor(None, tts.synthesize, body.text)
        return Response(wav, media_type="audio/wav",
                        headers={"x-tts-ms": str(int((time.monotonic() - t0) * 1000)), "cache-control": "no-store"})

    return app


# --------------------------------------------------------------------------- model download (explicit, one-off)

def download(settings: Settings) -> None:
    """Fetch the configured models into data/models (needs internet once)."""
    import httpx
    models_dir = settings.data_dir / "models"
    stt = SpeechToText(settings.voice_stt_model, models_dir, settings.voice_threads)
    print(f"Speech recognition model: faster-whisper {stt.name} …", flush=True)
    stt.load(allow_download=True)
    print(f"  {stt.state}{': ' + stt.error if stt.error else ''}")

    name = settings.voice_tts_voice                     # e.g. en_US-lessac-medium
    lang, speaker, quality = name.split("-", 2)
    family = lang.split("_")[0]
    dest = models_dir / "piper"
    dest.mkdir(parents=True, exist_ok=True)
    for suffix in (".onnx", ".onnx.json"):
        target = dest / f"{name}{suffix}"
        if target.exists():
            continue
        url = f"{PIPER_BASE}/{family}/{lang}/{speaker}/{quality}/{name}{suffix}"
        print(f"Voice file {target.name} …", flush=True)
        tmp = target.with_suffix(target.suffix + ".part")
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
        tmp.rename(target)
    tts = TextToSpeech(name, models_dir)
    tts.load()
    print(f"Voice {name}: {tts.state}{': ' + tts.error if tts.error else ''}")


def main() -> None:
    import os

    import uvicorn
    parser = argparse.ArgumentParser(description="AURA voice worker")
    parser.add_argument("--download", action="store_true", help="download the configured models and exit")
    args = parser.parse_args()
    os.umask(0o077)
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)        # model downloads log full URLs
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)
    if args.download:
        download(settings)
        return
    uvicorn.run(create_worker(settings), host="127.0.0.1", port=settings.voice_port,
                log_level="warning", server_header=False, proxy_headers=False)


if __name__ == "__main__":
    main()
