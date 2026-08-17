#!/usr/bin/env python3
"""Render the eval bassline with a *sampled* bass instrument instead of synthesis.

Why this exists: the earlier fixtures synthesise the bass as a harmonic stack.
That is clean, perfectly periodic, and has no attack transient, no string noise
and no decay character -- which is exactly what a pitch tracker sees. So while
those fixtures established that separation and segmentation work, they said
nothing about how the pipeline handles a real recorded bass tone.

This writes a MIDI file from the same PROGRESSION and PATTERN that
make_eval_track.py uses, so the ground truth is byte-identical and the only
variable is timbre. FluidSynth renders it through Apple's built-in General MIDI
soundbank, whose bass programs are sampled from real instruments -- no
third-party soundfont download, and reproducible on any Mac.

  python tools/make_sampled_bass.py --out bass.wav --program 33

GM bass programs: 32 acoustic, 33 electric finger, 34 electric pick,
35 fretless, 36 slap 1, 37 slap 2, 38 synth 1, 39 synth 2.

Honest limitation: Apple's DLS bank is 2MB, so the samples are short and
loop-based -- a real bass DI would be better still. This closes most of the
timbre gap, not all of it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_eval_track import PATTERN, PROGRESSION  # noqa: E402  reuse == identical truth

APPLE_DLS = (
    "/System/Library/Components/CoreAudio.component/Contents/Resources/gs_instruments.dls"
)
TICKS_PER_BEAT = 480


def _vlq(n: int) -> bytes:
    """MIDI variable-length quantity."""
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        out.insert(0, (n & 0x7F) | 0x80)
        n >>= 7
    return bytes(out)


def build_midi(bpm: float, bars: int, program: int, velocity: int) -> tuple[bytes, list[dict]]:
    """A single-track MIDI file plus the ground truth it encodes.

    Written by hand rather than with a library: the note list is trivial and this
    keeps the tool dependency-free, so it can run in CI alongside the others.
    """
    beat_s = 60.0 / bpm
    events: list[tuple[int, bytes]] = []  # (absolute tick, message)
    truth: list[dict] = []

    events.append((0, bytes([0xC0, program])))  # program change

    for bar in range(bars):
        root = PROGRESSION[bar % len(PROGRESSION)]
        for beat_off, interval, dur_beats in PATTERN:
            midi = root + interval
            on_beat = bar * 4 + beat_off
            on_tick = int(round(on_beat * TICKS_PER_BEAT))
            off_tick = int(round((on_beat + dur_beats) * TICKS_PER_BEAT))
            events.append((on_tick, bytes([0x90, midi, velocity])))
            events.append((off_tick, bytes([0x80, midi, 0])))
            truth.append(
                {
                    "onset": round(on_beat * beat_s, 4),
                    "duration": round(dur_beats * beat_s, 4),
                    "midi": int(midi),
                }
            )

    # Note-offs must precede note-ons at the same tick, or a repeated pitch gets
    # silenced by the previous note's off.
    events.sort(key=lambda e: (e[0], 0 if e[1][0] == 0x80 else 1))

    track = bytearray()
    tempo = int(round(60_000_000 / bpm))
    track += _vlq(0) + b"\xff\x51\x03" + struct.pack(">I", tempo)[1:]
    prev = 0
    for tick, msg in events:
        track += _vlq(tick - prev) + msg
        prev = tick
    track += _vlq(0) + b"\xff\x2f\x00"  # end of track

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, TICKS_PER_BEAT)
    chunk = b"MTrk" + struct.pack(">I", len(track)) + bytes(track)
    return header + chunk, truth


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="sampled_bass.wav")
    p.add_argument("--out-truth", default=None, help="also write the ground truth JSON")
    p.add_argument("--out-midi", default=None, help="keep the intermediate .mid")
    p.add_argument("--bpm", type=float, default=100.0)
    p.add_argument("--bars", type=int, default=32)
    p.add_argument("--program", type=int, default=33, help="GM program; 33 = electric bass (finger)")
    p.add_argument("--velocity", type=int, default=100)
    p.add_argument("--soundbank", default=APPLE_DLS)
    p.add_argument("--gain", type=float, default=0.8)
    a = p.parse_args()

    if not shutil.which("fluidsynth"):
        sys.exit("fluidsynth not found: brew install fluid-synth")
    if not Path(a.soundbank).exists():
        sys.exit(f"soundbank not found: {a.soundbank}")

    midi, truth = build_midi(a.bpm, a.bars, a.program, a.velocity)
    mid_path = Path(a.out_midi) if a.out_midi else Path(a.out).with_suffix(".mid")
    mid_path.write_bytes(midi)

    cmd = [
        "fluidsynth", "-ni", "-F", a.out, "-r", "44100",
        "-g", str(a.gain), a.soundbank, str(mid_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not Path(a.out).exists():
        sys.exit(f"fluidsynth failed:\n{res.stdout[-800:]}\n{res.stderr[-800:]}")

    if a.out_truth:
        Path(a.out_truth).write_text(
            json.dumps(
                {
                    "song": f"sampled-bass-gm{a.program}-{a.bars}bar-{int(a.bpm)}bpm",
                    "tempo_bpm": a.bpm,
                    "synthetic": True,
                    "bass_source": f"sampled -- GM program {a.program} via {Path(a.soundbank).name}",
                    "note": "Positions omitted deliberately: a rendered note has no correct "
                            "fingering, so position accuracy is not measurable from this fixture.",
                    "notes": truth,
                },
                indent=2,
            )
        )

    size = Path(a.out).stat().st_size
    pitches = [t["midi"] for t in truth]
    print(f"{a.out}: {size/1e6:.2f}MB")
    print(f"{len(truth)} notes, midi {min(pitches)}-{max(pitches)}, GM program {a.program}")
    if not a.out_midi:
        mid_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
