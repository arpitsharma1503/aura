"""Voice worker end-of-speech detection (no microphone or models needed)."""

import math
import random

from app.voice_worker import FRAME, RATE, Endpointer

FPS = RATE / FRAME   # frames per second (33.3)


def run(levels):
    ep = Endpointer()
    for i, r in enumerate(levels):
        state = ep.feed(r)
        if state != "continue":
            return state, ep, i
    return "max", ep, len(levels)


def room(seconds, rms=35):
    rnd = random.Random(1)
    return [rms * (0.8 + 0.4 * rnd.random()) for _ in range(int(seconds * FPS))]


def test_quiet_room_never_counts_as_speech():
    state, ep, _ = run(room(8))
    assert state == "no_speech" and not ep.heard_speech and not ep.maybe_speech


def test_distant_speech_is_detected_and_ends_after_a_pause():
    # measured on this Pi: room ~35 RMS; a voice a metre or two away ~150-300 RMS
    speech = [180 + 100 * math.sin(i / 3) ** 2 for i in range(int(1.5 * FPS))]
    state, ep, i = run(room(0.6) + speech + room(3))
    assert state == "done" and ep.heard_speech
    assert i / FPS < 0.6 + 1.5 + 1.5                   # stopped ~1.2 s after speaking, not at the limit


def test_old_fixed_minimum_is_gone():
    ep = Endpointer()
    for _ in range(10):
        ep.feed(35)
    assert ep.threshold() < 200                          # was 350, above typical distant speech


def test_noisy_room_scales_the_threshold():
    ep = Endpointer()
    for _ in range(10):
        ep.feed(220)
    assert ep.threshold() >= 500                          # fan or TV noise isn't mistaken for speech


def test_very_quiet_speech_gets_a_second_opinion():
    # too short/quiet for the detector, but clearly louder than the room
    blip = [110] * 5
    state, ep, _ = run(room(1) + blip + room(6))
    assert state == "no_speech" and not ep.heard_speech and ep.maybe_speech


def test_switch_on_click_is_ignored():
    state, ep, _ = run([3000, 2500] + room(7))
    assert not ep.maybe_speech
