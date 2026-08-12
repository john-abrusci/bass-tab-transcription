"""Segmentation tests that run anywhere -- no GPU, no torch, no audio.

Note segmentation is the one part of the worker whose quality we tune by hand,
so it needs to be testable without a 6GB image and a rented GPU. Each test
synthesises an f0 track with a specific pathology (vibrato, an octave blip, a
dropout) and asserts we recover the notes we put in.

Run: python test_segment.py
"""

import numpy as np

from segment import estimate_tempo, hz_to_midi, quantize_with_hysteresis, segment_notes

HOP = 0.01


def midi_to_hz(m):
    return 440.0 * 2.0 ** ((np.asarray(m, dtype=float) - 69.0) / 12.0)


def build_track(events, hop_s=HOP, conf=0.9, cents_noise=0.0, seed=0):
    """events: list of (midi_or_None, duration_s). None = silence."""
    rng = np.random.default_rng(seed)
    f0, per = [], []
    for pitch, dur in events:
        n = int(round(dur / hop_s))
        if pitch is None:
            f0.extend([0.0] * n)
            per.extend([0.05] * n)
        else:
            cents = rng.normal(0.0, cents_noise, n) if cents_noise else np.zeros(n)
            f0.extend(midi_to_hz(pitch + cents / 100.0).tolist())
            per.extend([conf] * n)
    return np.array(f0), np.array(per)


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  -- ' + detail if detail and not cond else ''}")
    return bool(cond)


def test_basic_run():
    f0, per = build_track([(40, 0.5), (None, 0.2), (45, 0.4), (43, 0.3)])
    notes = segment_notes(f0, per, HOP)
    ok = check("recovers three notes", len(notes) == 3, f"got {len(notes)}")
    ok &= check("pitches correct", [n["midi"] for n in notes] == [40, 45, 43],
                str([n["midi"] for n in notes]))
    ok &= check("first onset at 0", abs(notes[0]["onset"]) < 1e-6)
    ok &= check("second onset ~0.7s", abs(notes[1]["onset"] - 0.7) < 0.03,
                str(notes[1]["onset"]))
    ok &= check("duration ~0.5s", abs(notes[0]["duration"] - 0.5) < 0.03,
                str(notes[0]["duration"]))
    return ok


def test_vibrato_stays_one_note():
    # +/-40 cents wobble at ~5Hz: a plain round() splits this into a stutter.
    n = 100
    t = np.arange(n) * HOP
    cents = 40.0 * np.sin(2 * np.pi * 5.0 * t)
    f0 = midi_to_hz(40 + cents / 100.0)
    per = np.full(n, 0.9)
    notes = segment_notes(f0, per, HOP)
    return check("vibrato stays one note", len(notes) == 1 and notes[0]["midi"] == 40,
                 f"got {[(n['midi'], n['duration']) for n in notes]}")


def test_octave_blip_filtered():
    # The characteristic CREPE failure on bass: two frames jump an octave.
    f0, per = build_track([(40, 0.5)])
    f0[20:22] = midi_to_hz(52)
    notes = segment_notes(f0, per, HOP)
    return check("octave blip removed by median filter",
                 len(notes) == 1 and notes[0]["midi"] == 40,
                 f"got {[n['midi'] for n in notes]}")


def test_short_dropout_bridged():
    f0, per = build_track([(38, 0.3), (None, 0.02), (38, 0.3)])
    notes = segment_notes(f0, per, HOP)
    return check("20ms dropout does not split the note", len(notes) == 1,
                 f"got {len(notes)}")


def test_long_gap_splits():
    f0, per = build_track([(38, 0.3), (None, 0.25), (38, 0.3)])
    notes = segment_notes(f0, per, HOP)
    return check("250ms gap splits into two notes", len(notes) == 2, f"got {len(notes)}")


def test_short_notes_dropped():
    f0, per = build_track([(40, 0.03), (None, 0.1), (45, 0.4)])
    notes = segment_notes(f0, per, HOP)
    return check("30ms blip dropped as noise",
                 len(notes) == 1 and notes[0]["midi"] == 45, f"got {len(notes)}")


def test_confidence_gate():
    f0, per = build_track([(40, 0.5)], conf=0.3)
    notes = segment_notes(f0, per, HOP, conf_threshold=0.5)
    return check("low-periodicity frames gated out", notes == [], f"got {notes}")


def test_out_of_range_dropped():
    f0, per = build_track([(88, 0.5)])  # way above any bass fundamental
    notes = segment_notes(f0, per, HOP)
    return check("out-of-range pitch dropped", notes == [], f"got {notes}")


def test_hysteresis_allows_half_step():
    midi = np.array([40.0] * 20 + [41.0] * 20)
    q = quantize_with_hysteresis(midi)
    return check("half-step move still switches", q[-1] == 41.0 and q[0] == 40.0,
                 f"got {q[0]} -> {q[-1]}")


def test_tempo_estimate():
    # 120bpm eighth notes = 0.25s inter-onset.
    events = []
    for i in range(16):
        events.append((40 + (i % 3), 0.2))
        events.append((None, 0.05))
    f0, per = build_track(events)
    notes = segment_notes(f0, per, HOP)
    tempo = estimate_tempo([n["onset"] for n in notes])
    return check("tempo lands in a musical range for 0.25s IOIs",
                 tempo is not None and 60 <= tempo <= 180, str(tempo))


def test_hz_to_midi():
    return check("A440 -> midi 69", abs(hz_to_midi(np.array([440.0]))[0] - 69.0) < 1e-9)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"running {len(tests)} segmentation tests\n")
    results = []
    for t in tests:
        print(t.__name__)
        results.append(bool(t()))
    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed")
    raise SystemExit(0 if passed == len(results) else 1)
