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

from .audio import FRAME, RATE, Endpointer, dbfs, rms_of  # noqa: F401  (re-exported for tests)
from .config import Settings, get_settings
from .security import is_loopback
from .wake import WakeListener

log = logging.getLogger("aura.voice")

MAX_SECONDS = 15
CLIP_TTL = 120                   # seconds an untranscribed clip may stay in memory
PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"


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

    def transcribe(self, pcm: bytes, vad: bool = False, prompt: str | None = None) -> str:
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if 0 < peak < 0.5:
            audio = audio * min(0.9 / peak, 20.0)       # quiet, distant speech: bring it up to a normal level
        segments, _info = self._model.transcribe(
            audio, language="en", beam_size=1, vad_filter=vad, condition_on_previous_text=False,
            initial_prompt=prompt or "Aura, weather, rain, umbrella, commute, office, traffic, calendar, birthday, groceries.")
        text = " ".join(s.text.strip() for s in segments).strip()
        if vad and text.lower().strip(" .!?") in HALLUCINATIONS:
            return ""                                     # Whisper's usual words for noise, not speech
        return text


HALLUCINATIONS = {"", "you", "thank you", "thanks", "thanks for watching", "thank you for watching", "bye", "okay"}


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
    uncertain: bool = False          # quiet: let Whisper's VAD decide whether it is speech


class Recorder:
    def __init__(self, device: str) -> None:
        self.device = device
        self.proc: asyncio.subprocess.Process | None = None
        self._stop = asyncio.Event()
        self._cancel = False
        self.level = -90.0
        self.wake = None                 # WakeListener, when the wake word is on

    def _shared(self) -> bool:
        return bool(self.wake and self.wake.running)

    @property
    def busy(self) -> bool:
        return self.proc is not None or bool(self.wake and self.wake.capturing)

    def argv(self, max_seconds: int) -> list[str]:
        return ["arecord", "-D", self.device, "-f", "S16_LE", "-r", str(RATE), "-c", "1",
                "-t", "raw", "-q", "-d", str(max_seconds), "-"]

    async def record(self, max_seconds: int) -> tuple[bytes, Endpointer, str]:
        if self.busy:
            raise HTTPException(409, "Already listening.")
        if self._shared():
            try:
                return await self.wake.capture(max_seconds)
            except RuntimeError as exc:
                raise HTTPException(409, "Already listening.") from exc
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
        if self.wake:
            self.wake.stop_capture()

    def cancel(self) -> None:
        self._cancel = True
        self._stop.set()
        if self.wake:
            self.wake.stop_capture(cancel=True)


# --------------------------------------------------------------------------- HTTP API (loopback only)

class RecordIn(BaseModel):
    max_seconds: int = Field(default=10, ge=2, le=MAX_SECONDS)


class WakeIn(BaseModel):
    enabled: bool
    max_seconds: int = Field(default=10, ge=2, le=MAX_SECONDS)
    notify_url: str = Field(pattern=r"^http://127\.0\.0\.1:\d{2,5}/api/assistant/wake$")


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=1200)


def create_worker(settings: Settings, load_models: bool = True) -> FastAPI:
    models_dir = settings.data_dir / "models"
    stt = SpeechToText(settings.voice_stt_model, models_dir, settings.voice_threads)
    tts = TextToSpeech(settings.voice_tts_voice, models_dir)
    rec = Recorder(settings.voice_mic_device)
    clips: dict[str, Clip] = {}
    busy = asyncio.Lock()             # one transcription / synthesis at a time (CPU bound)
    notify_url: dict[str, str | None] = {"url": None}

    async def transcribe_async(pcm: bytes, vad: bool, wake_check: bool = False) -> str:
        if stt.state != "ready":
            raise RuntimeError("speech recognition not ready")
        prompt = "Hey Aura." if wake_check else None      # helps Whisper with a short, isolated "Hey Aura"
        async with busy:
            return await asyncio.get_running_loop().run_in_executor(None, stt.transcribe, pcm, vad, prompt)

    async def notify(event: dict) -> None:
        if not notify_url["url"]:
            return
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(notify_url["url"], json=event)

    wake = WakeListener(settings.voice_mic_device, models_dir / "vosk" / "small-en-us", transcribe_async, notify,
                        phrase=settings.voice_wake_phrase)
    rec.wake = wake

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
        await wake.set_enabled(False)

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
            "wake": wake.status(),
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
        levels = ep.levels()
        if reason == "cancelled":
            return {"clip_id": None, "reason": reason, "seconds": secs, "levels": levels}
        uncertain = not ep.heard_speech
        if uncertain and not ep.maybe_speech:
            return {"clip_id": None, "reason": "no_speech", "seconds": secs, "levels": levels}
        sweep()
        cid = secrets.token_urlsafe(9)
        clips[cid] = Clip(pcm, time.monotonic(), secs, uncertain=uncertain)
        return {"clip_id": cid, "reason": reason, "seconds": secs, "levels": levels, "uncertain": uncertain,
                "record_ms": int((time.monotonic() - t0) * 1000)}

    @app.post("/wake")
    async def wake_config(body: WakeIn) -> dict:
        """Turn the wake word on/off (sent by the dashboard from Settings → Assistant)."""
        notify_url["url"] = body.notify_url
        await wake.set_enabled(body.enabled, body.max_seconds)
        return wake.status()

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
            text = await asyncio.get_running_loop().run_in_executor(None, stt.transcribe, clip.pcm, clip.uncertain)
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

    vosk_dir = models_dir / "vosk" / "small-en-us"
    if not vosk_dir.exists():
        import tempfile
        import zipfile
        print("Wake-word model vosk-model-small-en-us-0.15 …", flush=True)
        vosk_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=vosk_dir.parent) as tmp:
            z = Path(tmp) / "m.zip"
            with httpx.stream("GET", VOSK_URL, follow_redirects=True, timeout=120) as r:
                r.raise_for_status()
                with open(z, "wb") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        f.write(chunk)
            with zipfile.ZipFile(z) as zf:
                zf.extractall(tmp)
            (Path(tmp) / "vosk-model-small-en-us-0.15").rename(vosk_dir)
    print(f"Wake-word model: {'ready' if (vosk_dir / 'README').exists() else 'missing'}")


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
