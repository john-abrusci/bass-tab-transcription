#!/usr/bin/env python3
"""Generate an evaluation track with exact ground truth.

The problem this solves: scraped human tabs carry no timestamps, so onset-based
F1 against them is impossible without first aligning tab to audio, which is its
own project. Here the bassline is synthesised, so every onset and pitch is known
exactly by construction.

The backing is synthesised too -- drums and mid-register chords -- for two
reasons. Mixing a synthetic bass under a real song would contaminate the truth,
because the song already contains bass that Demucs would also separate. And a
fully synthetic track is reproducible in CI and carries no copyright.

  python tools/make_eval_track.py --out-audio eval_track.wav --out-truth truth.json
  python tools/make_eval_track.py --bars 32 --bpm 100 --no-backing   # bass alone

What this measures and what it does not:

  measures    note-level F1, pitch accuracy, octave-error rate, onset timing --
              the pitch-tracking and segmentation stages, against a separation
              problem that is real but easier than recorded music.
  does NOT    position accuracy. A synthesised note has no "correct" fingering,
              so the truth file deliberately carries no string/fret. That still
              needs human tabs.

Separation will look easier here than on real music: no fret noise, no amp
character, no dynamics, no room. Treat the resulting F1 as a ceiling.
"""

from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path

import numpy as np

SR = 44_100

# A 16-bar progression with a bit of movement. Roots as MIDI, one per bar.
# Sits in E minor-ish territory: the register a real bassline occupies.
PROGRESSION = [40, 40, 45, 45, 47, 47, 45, 45, 40, 40, 43, 43, 45, 47, 40, 40]

# (beat offset, interval above root, duration in beats). Root-fifth-octave
# movement with an eighth-note pickup -- enough rhythmic variety that the
# segmenter has to find real onsets rather than one sustained tone per bar.
PATTERN = [
    (0.0, 0, 0.9),
    (1.0, 7, 0.9),
    (2.0, 12, 0.45),
    (2.5, 7, 0.45),
    (3.0, 0, 0.45),
    (3.5, 3, 0.45),
]


def midi_to_hz(m: float) -> float:
    return 440.0 * 2.0 ** ((m - 69.0) / 12.0)


def pluck(midi: float, dur_s: float, sr: int = SR) -> np.ndarray:
    """Harmonic stack with a plucked envelope -- see make_test_tone.py."""
    n = int(dur_s * sr)
    t = np.arange(n) / sr
    f0 = midi_to_hz(midi)
    out = np.zeros(n)
    for k, amp in enumerate([1.0, 0.5, 0.32, 0.18, 0.1], start=1):
        if f0 * k < sr / 2:
            out += amp * np.sin(2 * np.pi * f0 * k * t)
    return out * np.minimum(t / 0.008, 1.0) * np.exp(-t * 1.8)


def build_bass(bpm: float, bars: int) -> tuple[np.ndarray, list[dict]]:
    beat = 60.0 / bpm
    total = int(bars * 4 * beat * SR) + SR
    audio = np.zeros(total)
    truth: list[dict] = []

    for bar in range(bars):
        root = PROGRESSION[bar % len(PROGRESSION)]
        bar_start = bar * 4 * beat
        for beat_off, interval, dur_beats in PATTERN:
            onset = bar_start + beat_off * beat
            dur = dur_beats * beat
            midi = root + interval
            seg = pluck(midi, dur)
            i = int(onset * SR)
            audio[i : i + len(seg)] += seg
            truth.append(
                {
                    "onset": round(onset, 4),
                    "duration": round(dur, 4),
                    "midi": int(midi),
                }
            )
    return audio, truth


def build_drums(bpm: float, bars: int, n: int, seed: int = 7) -> np.ndarray:
    """Kick, snare and hats. Gives separation something real to pull apart."""
    rng = np.random.default_rng(seed)
    beat = 60.0 / bpm
    out = np.zeros(n)

    def place(sig: np.ndarray, at_s: float, gain: float) -> None:
        i = int(at_s * SR)
        end = min(i + len(sig), n)
        if i < n:
            out[i:end] += sig[: end - i] * gain

    kt = np.arange(int(0.13 * SR)) / SR
    # Pitch-swept sine: the standard synthesised kick.
    kick = np.sin(2 * np.pi * (110 * np.exp(-kt * 32) + 42) * kt) * np.exp(-kt * 26)
    st = np.arange(int(0.16 * SR)) / SR
    snare = rng.normal(0, 1, len(st)) * np.exp(-st * 24)
    snare += np.sin(2 * np.pi * 190 * st) * np.exp(-st * 26) * 0.6
    ht = np.arange(int(0.045 * SR)) / SR
    hat = rng.normal(0, 1, len(ht)) * np.exp(-ht * 130)

    for bar in range(bars):
        b0 = bar * 4 * beat
        place(kick, b0, 0.95)
        place(kick, b0 + 2.5 * beat, 0.75)
        place(snare, b0 + beat, 0.55)
        place(snare, b0 + 3 * beat, 0.55)
        for eighth in range(8):
            place(hat, b0 + eighth * beat / 2, 0.22)
    return out


def build_chords(bpm: float, bars: int, n: int) -> np.ndarray:
    """Sustained triads two octaves above the bass.

    Kept well clear of the bass register on purpose: the point is to give the
    separator a competing harmonic source, not to make pitch tracking ambiguous
    in a way that would confound what the eval is measuring.
    """
    beat = 60.0 / bpm
    out = np.zeros(n)
    for bar in range(bars):
        root = PROGRESSION[bar % len(PROGRESSION)] + 24
        dur = 4 * beat
        t = np.arange(int(dur * SR)) / SR
        chord = np.zeros(len(t))
        for interval in (0, 3, 7, 12):
            f = midi_to_hz(root + interval)
            for k, amp in enumerate([1.0, 0.4, 0.2], start=1):
                if f * k < SR / 2:
                    chord += amp * np.sin(2 * np.pi * f * k * t) / 4
        env = np.minimum(t / 0.05, 1.0) * np.exp(-t * 0.7)
        i = int(bar * 4 * beat * SR)
        end = min(i + len(chord), n)
        out[i:end] += (chord * env)[: end - i]
    return out


def write_wav(path: Path, audio: np.ndarray) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    stereo = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(stereo.tobytes())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out-audio", default="eval_track.wav")
    p.add_argument("--out-truth", default="eval_truth.json")
    p.add_argument("--bpm", type=float, default=100.0)
    p.add_argument("--bars", type=int, default=32)
    p.add_argument("--no-backing", action="store_true", help="bass alone, no drums or chords")
    p.add_argument("--bass-gain", type=float, default=1.0)
    p.add_argument("--backing-gain", type=float, default=0.85)
    a = p.parse_args()

    bass, truth = build_bass(a.bpm, a.bars)
    bass = bass / max(np.abs(bass).max(), 1e-9)
    mix = bass * a.bass_gain

    if not a.no_backing:
        n = len(bass)
        backing = build_drums(a.bpm, a.bars, n) + build_chords(a.bpm, a.bars, n)
        backing = backing / max(np.abs(backing).max(), 1e-9)
        mix = mix + backing * a.backing_gain

    mix = mix / max(np.abs(mix).max(), 1e-9) * 0.89

    out_audio = Path(a.out_audio)
    write_wav(out_audio, mix)
    Path(a.out_truth).write_text(
        json.dumps(
            {
                "song": f"synthetic-eval-{a.bars}bar-{int(a.bpm)}bpm"
                + ("-bass-only" if a.no_backing else ""),
                "tempo_bpm": a.bpm,
                "synthetic": True,
                "note": "Positions omitted deliberately: a synthesised note has no correct "
                        "fingering, so position accuracy is not measurable from this fixture.",
                "notes": truth,
            },
            indent=2,
        )
    )

    pitches = [t["midi"] for t in truth]
    print(f"{out_audio}: {len(mix)/SR:.1f}s, {out_audio.stat().st_size/1e6:.2f}MB")
    print(f"{a.out_truth}: {len(truth)} notes, midi {min(pitches)}-{max(pitches)}")
    print(f"  {a.bars} bars @ {a.bpm}bpm, backing: {'no' if a.no_backing else 'drums + chords'}")


if __name__ == "__main__":
    main()
