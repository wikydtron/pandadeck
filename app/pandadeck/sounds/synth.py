"""Meme-style soundboard sounds, synthesized from scratch (pure Python).

Every default sound on the SOUNDS deck page is generated here — no samples, no
recordings — so they are original works that can ship with PandaDeck (the famous
originals are copyrighted). Users can replace any of them with their own file.

    LIBRARY[id] -> (label, icon)      icon = a firmware icon name
    render(id)  -> bytes              16-bit mono WAV, deterministic
"""
from __future__ import annotations

import io
import math
import random
import wave
from typing import Callable

SR = 44100
TAU = 2 * math.pi

LIBRARY: dict[str, tuple[str, str]] = {
    "airhorn": ("Air horn", "vol_up"),
    "vineboom": ("Boom", "bolt"),
    "sadtrombone": ("Sad trombone", "vol_down"),
    "rimshot": ("Ba dum tss", "music"),
    "dramatic": ("Dun dun DUN", "warning"),
    "crickets": ("Crickets", "cloud"),
    "scratch": ("Scratch", "loop"),
    "tada": ("Ta-da!", "check"),
    "wrong": ("Wrong!", "close"),
    "coin": ("Coin", "game"),
    "boing": ("Boing", "smile"),
    "drumroll": ("Drum roll", "audio"),
}


# ---- building blocks (lists of floats in -1..1) ------------------------------------------

def silence(sec: float) -> list[float]:
    return [0.0] * int(sec * SR)


def n_samples(sec: float) -> int:
    return max(1, int(sec * SR))


def osc(freq: Callable[[float], float] | float, sec: float, shape: str = "sine", phase: float = 0.0) -> list[float]:
    """Oscillator with a constant or time-varying frequency (phase-accumulated)."""
    f = freq if callable(freq) else (lambda _t, c=freq: c)
    out, ph = [], phase
    for i in range(n_samples(sec)):
        ph += f(i / SR) / SR
        p = ph % 1.0
        if shape == "sine":
            v = math.sin(TAU * p)
        elif shape == "saw":
            v = 2.0 * p - 1.0
        elif shape == "square":
            v = 1.0 if p < 0.5 else -1.0
        else:  # triangle
            v = 4.0 * abs(p - 0.5) - 1.0
        out.append(v)
    return out


def noise(sec: float, seed: int) -> list[float]:
    rnd = random.Random(seed)
    return [rnd.uniform(-1.0, 1.0) for _ in range(n_samples(sec))]


def lowpass(x: list[float], fc: float) -> list[float]:
    a = 1.0 - math.exp(-TAU * fc / SR)
    y, out = 0.0, []
    for v in x:
        y += a * (v - y)
        out.append(y)
    return out


def highpass(x: list[float], fc: float) -> list[float]:
    lp = lowpass(x, fc)
    return [a - b for a, b in zip(x, lp)]


def env(x: list[float], attack: float = 0.005, release: float = 0.05, decay: float | None = None) -> list[float]:
    """Linear attack/release; optional exponential decay (time constant in seconds)."""
    n = len(x)
    a, r = max(1, int(attack * SR)), max(1, int(release * SR))
    out = []
    for i, v in enumerate(x):
        g = min(1.0, i / a, (n - i) / r)
        if decay:
            g *= math.exp(-(i / SR) / decay)
        out.append(v * g)
    return out


def gain(x: list[float], g: float) -> list[float]:
    return [v * g for v in x]


def drive(x: list[float], amount: float) -> list[float]:
    """Soft clipping (tanh) for grit."""
    k = math.tanh(amount)
    return [math.tanh(v * amount) / k for v in x]


def mix(*parts: tuple[float, list[float]]) -> list[float]:
    """Mix (start_seconds, samples) pairs."""
    end = max(int(s * SR) + len(p) for s, p in parts)
    out = [0.0] * end
    for s, p in parts:
        o = int(s * SR)
        for i, v in enumerate(p):
            out[o + i] += v
    return out


def add(*layers: list[float]) -> list[float]:
    n = max(len(l) for l in layers)
    return [sum(l[i] for l in layers if i < len(l)) for i in range(n)]


def echo(x: list[float], delay: float, feedback: float, wet: float) -> list[float]:
    d = int(delay * SR)
    buf = x + [0.0] * int(delay * SR * 6)
    out = list(buf)
    for i in range(d, len(out)):
        out[i] += out[i - d] * feedback
    return [a * (1 - wet) + b * wet for a, b in zip(buf, out)]


def chord(freqs: list[float], sec: float, shape: str, detune: float = 0.0) -> list[float]:
    layers = []
    for f in freqs:
        layers.append(osc(f * (1 + detune), sec, shape))
        if detune:
            layers.append(osc(f * (1 - detune), sec, shape, phase=0.37))
    return gain(add(*layers), 1.0 / len(layers))


def note(name: str) -> float:
    """'A4' -> 440 Hz (sharps with #)."""
    names = {"C": -9, "C#": -8, "D": -7, "D#": -6, "E": -5, "F": -4, "F#": -3, "G": -2, "G#": -1, "A": 0,
             "A#": 1, "B": 2}
    pitch, octave = name[:-1], int(name[-1])
    return 440.0 * 2 ** ((names[pitch] + 12 * (octave - 4)) / 12)


# ---- the sounds ------------------------------------------------------------------------------

def airhorn() -> list[float]:
    def blast(sec: float) -> list[float]:
        bend = lambda t: 1.0 + 0.04 * math.exp(-t * 25)  # noqa: E731 - the "honk" scoop
        tone = add(*(gain(osc(lambda t, f=f: f * bend(t), sec, "saw"), g)
                     for f, g in ((466.2, 1.0), (470.0, 0.8), (932.4, 0.35), (699.0, 0.3))))
        return env(drive(lowpass(tone, 3200), 3.0), 0.004, 0.03)
    s = 0.0
    parts = []
    for sec in (0.11, 0.11, 0.11):
        parts.append((s, blast(sec)))
        s += sec + 0.045
    parts.append((s, blast(0.75)))
    return mix(*parts)


def vineboom() -> list[float]:
    sec = 1.6
    sweep = lambda t: 42 + 130 * math.exp(-t * 28)  # noqa: E731
    body = env(osc(sweep, sec, "sine"), 0.001, 0.2, decay=0.45)
    click = env(highpass(noise(0.012, 7), 1500), 0.0005, 0.008)
    thump = drive(add(body, gain(click, 0.5)), 2.6)
    return echo(lowpass(thump, 1800), 0.075, 0.35, 0.3)


def sadtrombone() -> list[float]:
    parts, s = [], 0.0
    for name, sec, vib in (("G3", 0.42, 0.0), ("F#3", 0.42, 0.0), ("F3", 0.42, 0.0), ("E3", 1.5, 0.035)):
        f0 = note(name)
        f = (lambda t, f0=f0, vib=vib: f0 * (1 + vib * min(1.0, t / 0.4) * math.sin(TAU * 5.5 * t)))
        tone = add(osc(f, sec, "saw"), gain(osc(f, sec, "square"), 0.25))
        wah = lowpass(tone, 900 if sec < 1 else 700)
        parts.append((s, env(wah, 0.04, 0.12 if sec < 1 else 0.5)))
        s += sec + 0.02
    return drive(mix(*parts), 1.6)


def _tom(f0: float, sec: float) -> list[float]:
    return env(osc(lambda t: f0 * (1 + 0.6 * math.exp(-t * 30)), sec, "sine"), 0.001, 0.05, decay=0.12)


def _cymbal(sec: float, seed: int, decay: float) -> list[float]:
    return env(highpass(noise(sec, seed), 5000), 0.001, 0.2, decay=decay)


def rimshot() -> list[float]:
    snare = lambda seed: add(env(highpass(noise(0.18, seed), 1800), 0.001, 0.05, decay=0.05),  # noqa: E731
                             gain(_tom(190, 0.18), 0.6))
    return mix((0.0, snare(1)), (0.2, gain(_tom(110, 0.3), 0.9)), (0.44, gain(_cymbal(1.3, 2, 0.35), 0.8)),
               (0.44, gain(_tom(70, 0.4), 0.7)))


def dramatic() -> list[float]:
    def hit(freqs, sec, release):
        brass = chord(freqs, sec, "saw", detune=0.004)
        timp = env(osc(freqs[0] / 2, sec, "sine"), 0.002, release, decay=0.6)
        return env(lowpass(add(brass, gain(timp, 0.8)), 1400), 0.012, release)
    d = [note("D2"), note("A2"), note("D3"), note("F3")]
    low = [note("C#2"), note("G#2"), note("C#3"), note("E3")]
    trem = hit(low, 1.8, 0.9)
    trem = [v * (0.82 + 0.18 * math.sin(TAU * 6 * i / SR)) for i, v in enumerate(trem)]
    return drive(mix((0.0, hit(d, 0.24, 0.06)), (0.32, hit(d, 0.24, 0.06)), (0.66, trem)), 1.8)


def crickets() -> list[float]:
    def chirp(seed):
        pulses = []
        for k in range(3):
            tone = osc(4400 + 60 * k, 0.028, "sine")
            pulses.append((k * 0.045, env(tone, 0.003, 0.01)))
        return add(mix(*pulses), gain(lowpass(noise(0.14, seed), 3000), 0.02))
    parts = [(t, chirp(i)) for i, t in enumerate((0.0, 0.42, 0.84, 1.45, 1.87))]
    return echo(mix(*parts), 0.11, 0.25, 0.25)


def scratch() -> list[float]:
    sec = 0.62
    def f(t):  # two "wicka" strokes: fast back-and-forth pitch sweeps
        stroke = (t % 0.155) / 0.155
        return 250 + 1700 * math.sin(math.pi * stroke) ** 2
    tone = osc(f, sec, "saw")
    grit = highpass(noise(sec, 11), 800)
    body = [a * 0.7 + b * 0.35 for a, b in zip(tone, grit)]
    shaped = [v * abs(math.sin(math.pi * ((i / SR) % 0.155) / 0.155)) for i, v in enumerate(body)]
    return env(lowpass(drive(shaped, 2.0), 5000), 0.003, 0.05)


def tada() -> list[float]:
    def horn(name, sec, release):
        f0 = note(name)
        f = lambda t, f0=f0: f0 * (1 + 0.012 * min(1, t / 0.3) * math.sin(TAU * 5.8 * t))  # noqa: E731
        return env(lowpass(add(osc(f, sec, "saw"), gain(osc(f, sec, "square"), 0.3)), 2600), 0.01, release)
    parts = [(0.0, horn("C5", 0.12, 0.03)), (0.12, horn("E5", 0.12, 0.03)), (0.24, horn("G5", 0.12, 0.03))]
    final = add(horn("C5", 1.3, 0.6), horn("E5", 1.3, 0.6), horn("G5", 1.3, 0.6), horn("C6", 1.3, 0.6))
    parts.append((0.38, gain(final, 0.5)))
    return mix(*parts)


def wrong() -> list[float]:
    sec = 0.95
    buzz = add(osc(110, sec, "square"), osc(116.5, sec, "square"), gain(osc(55, sec, "saw"), 0.6))
    return env(lowpass(drive(buzz, 1.5), 1600), 0.005, 0.08)


def coin() -> list[float]:
    a = env(osc(note("B5"), 0.075, "square"), 0.001, 0.01)
    b = env(osc(note("E6"), 0.55, "square"), 0.001, 0.05, decay=0.18)
    return gain(lowpass(mix((0.0, a), (0.075, b)), 7000), 0.6)


def boing() -> list[float]:
    sec = 1.0
    def f(t):
        base = 140 + 160 * (1 - math.exp(-t * 6))
        return base * (1 + 0.35 * math.exp(-t * 3.2) * math.sin(TAU * 13 * t))
    tone = add(osc(f, sec, "sine"), gain(osc(lambda t: 2 * f(t), sec, "triangle"), 0.2))
    return env(tone, 0.002, 0.15, decay=0.35)


def drumroll() -> list[float]:
    parts, t, k = [], 0.0, 0
    while t < 1.7:
        level = 0.25 + 0.75 * (t / 1.7) ** 1.5
        hitn = env(highpass(noise(0.05, 100 + k), 1500), 0.001, 0.01, decay=0.025)
        parts.append((t, gain(hitn, level)))
        t += 0.032 - 0.004 * math.sin(k)
        k += 1
    parts.append((1.72, gain(_cymbal(1.6, 5, 0.5), 1.1)))
    parts.append((1.72, gain(_tom(60, 0.6), 1.2)))
    return mix(*parts)


GENERATORS: dict[str, Callable[[], list[float]]] = {
    "airhorn": airhorn, "vineboom": vineboom, "sadtrombone": sadtrombone, "rimshot": rimshot,
    "dramatic": dramatic, "crickets": crickets, "scratch": scratch, "tada": tada, "wrong": wrong,
    "coin": coin, "boing": boing, "drumroll": drumroll,
}


TARGET_RMS = 0.28   # loudness of each sound's loudest quarter second (similar volume across buttons)
CEILING = 0.94      # soft-limited peak


def loudest_rms(samples: list[float], window: float = 0.25) -> float:
    n = max(1, int(window * SR))
    best = 0.0
    for start in range(0, max(1, len(samples) - n + 1), n // 2 or 1):
        seg = samples[start:start + n]
        best = max(best, math.sqrt(sum(v * v for v in seg) / len(seg)))
    return best


def to_wav(samples: list[float]) -> bytes:
    """Loudness-match (loudest window -> TARGET_RMS), soft-limit the peaks, and
    encode as 16-bit mono PCM WAV."""
    g = TARGET_RMS / (loudest_rms(samples) or 1.0)
    frames = bytearray()
    for v in samples:
        x = v * g
        if abs(x) > 0.7 * CEILING:  # gentle knee: untouched below, tanh above
            x = math.copysign(0.7 * CEILING + (CEILING * 0.3) * math.tanh((abs(x) - 0.7 * CEILING) / (0.3 * CEILING)), x)
        s = int(max(-1.0, min(1.0, x)) * 32767)
        frames += s.to_bytes(2, "little", signed=True)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def render(sound_id: str) -> bytes:
    if sound_id not in GENERATORS:
        raise KeyError(sound_id)
    return to_wav(GENERATORS[sound_id]())
