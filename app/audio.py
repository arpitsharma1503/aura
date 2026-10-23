"""Shared audio helpers for the voice worker and the wake-word listener."""

from __future__ import annotations

import array
import math
from dataclasses import dataclass, field
from typing import Literal

RATE = 16_000
FRAME = 480                      # 30 ms of 16-bit mono


# --------------------------------------------------------------------------- end of speech detection

@dataclass
class Endpointer:
    """Simple energy-based end-of-speech detection for push-to-talk.

    Learns the room's noise floor from the first ~0.3 s, treats frames well above
    it as speech, and stops after ``silence_s`` of quiet once speech has started,
    or after ``no_speech_s`` if nobody speaks at all.
    """
    silence_s: float = 1.2
    no_speech_s: float = 6.0
    min_speech_s: float = 0.25
    floor: float | None = None
    frames: int = 0
    speech_frames: int = 0
    quiet_frames: int = 0
    peak: float = 0.0
    calib: list[float] = field(default_factory=list)

    def threshold(self) -> float:
        # Relative to the room: a cheap USB mic a metre or two away gives speech only a few
        # times louder than a quiet room (measured: room ~35 RMS), so no high fixed minimum.
        floor = self.floor if self.floor is not None else 60
        return max(floor * 2.5, floor + 80, 120)

    @property
    def maybe_speech(self) -> bool:
        """Clearly louder than the room at some point, even if not for long enough to
        count as speech. Such clips get a second opinion from Whisper's own VAD."""
        floor = self.floor if self.floor is not None else 60
        return self.peak >= max(floor * 1.8, floor + 40, 90)

    def levels(self) -> dict:
        return {"floor_dbfs": dbfs(self.floor or 0), "peak_dbfs": dbfs(self.peak), "threshold_dbfs": dbfs(self.threshold())}

    def feed(self, rms: float) -> Literal["continue", "done", "no_speech"]:
        self.frames += 1
        if self.floor is not None or len(self.calib) >= 3:
            self.peak = max(self.peak, rms)            # skip the first frames: the mic's switch-on click
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
