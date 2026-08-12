#!/usr/bin/env python3
"""Synthesise a short bass phrase with known pitches.

The point is a self-verifying end-to-end test: because we know exactly which
MIDI notes went in, the worker's output can be checked rather than merely
eyeballed. If a run of this comes back with the right notes at the right
onsets, separation, pitch tracking and segmentation are all provably working.

It also means the pipeline can be exercised without using copyrighted audio,
which matters for CI and for anyone reproducing this.

  python tools/make_test_tone.py                    # -> test_tone.wav
  python tools/make_test_tone.py --notes 28,33,38,43 --note-s 0.6

Needs numpy only; the wav is written with the stdlib.
"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np

SR = 44_100
# Open strings of a 4-string bass in standard tuning: E1 A1 D2 G2.
DEFAULT_NOTES = [28, 33, 38, 43]


def midi_to_hz(midi: float) -> float:
    return 440.0 * 2.0 ** ((midi - 69.0) / 12.0)


def pluck(midi: int, duration_s: float, sr: int = SR) -> np.ndarray:
    """A plucked-bass-ish tone: harmonic stack with a decaying envelope.

    A pure sine would be an unrealistically easy target -- real basses are rich
    in harmonics, and the harmonics are exactly what make an octave error
    tempting for a pitch tracker. Including them keeps the test honest.
    """
    n = int(duration_s * sr)
    t = np.arange(n) / sr
    f0 = midi_to_hz(midi)

    # Harmonic amplitudes roughly following a plucked string: strong fundamental,
    # rolling off but with real energy up to the fifth partial.
    wave_out = np.zeros(n)
    for k, amp in enumerate([1.0, 0.5, 0.32, 0.18, 0.1], start=1):
        if f0 * k >= sr / 2:
            break
        wave_out += amp * np.sin(2 * np.pi * f0 * k * t)

    # Fast attack, exponential decay -- gives the segmenter a real onset to find.
    attack = np.minimum(t / 0.008, 1.0)
    decay = np.exp(-t * 2.2)
    return wave_out * attack * decay


def build(notes, note_s: float, gap_s: float, sr: int = SR) -> tuple[np.ndarray, list]:
    segments = []
    expected = []
    t = 0.0
    for midi in notes:
        segments.append(pluck(midi, note_s, sr))
        expected.append({"onset": round(t, 4), "midi": int(midi)})
        t += note_s
        if gap_s > 0:
            segments.append(np.zeros(int(gap_s * sr)))
            t += gap_s
    audio = np.concatenate(segments)
    peak = np.abs(audio).max()
    if peak > 0:
        audio = audio / peak * 0.89  # leave headroom, avoid clipping on encode
    return audio, expected


def write_wav(path: Path, audio: np.ndarray, sr: int = SR) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    stereo = np.repeat(pcm[:, None], 2, axis=1)  # demucs expects stereo
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(stereo.tobytes())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="test_tone.wav")
    p.add_argument("--notes", default=",".join(str(n) for n in DEFAULT_NOTES))
    p.add_argument("--note-s", type=float, default=0.6)
    p.add_argument("--gap-s", type=float, default=0.2)
    a = p.parse_args()

    notes = [int(x) for x in a.notes.split(",") if x.strip()]
    audio, expected = build(notes, a.note_s, a.gap_s)
    out = Path(a.out)
    write_wav(out, audio)

    print(f"{out}: {len(audio)/SR:.2f}s, {out.stat().st_size/1e6:.2f}MB")
    print("expected notes:")
    for e in expected:
        print(f"  onset {e['onset']:5.2f}s  midi {e['midi']}")


if __name__ == "__main__":
    main()
