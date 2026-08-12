"""f0 frames -> note events.

torchcrepe hands back a per-frame (f0_hz, periodicity) pair at a ~10ms hop.
This module turns that into note events, in the six steps from the spec:

  1. gate on confidence (periodicity)
  2. convert to MIDI
  3. median filter to kill octave jumps
  4. quantize to semitones with a hysteresis band
  5. group runs into notes, drop the short ones
  6. cheap tempo estimate from inter-onset intervals

Steps 3 and 4 are where output quality actually lives, so everything there is
a named constant with a keyword override rather than a magic number inline.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence

import numpy as np

A4_HZ = 440.0
A4_MIDI = 69.0

# --- tuning knobs -----------------------------------------------------------
MEDIAN_WINDOW = 5  # frames; 5 @ 10ms hop = 50ms, enough to swallow an octave blip
HYSTERESIS_ST = 0.6  # extra semitones beyond the +/-0.5 rounding edge before we switch
SWITCH_FRAMES = 2  # consecutive frames agreeing on a new pitch before we commit
MIN_NOTE_S = 0.06  # anything shorter is noise, not a note
MAX_GAP_S = 0.04  # unvoiced gap this short at the same pitch = one note, not two


def hz_to_midi(f0_hz: np.ndarray) -> np.ndarray:
    """Vectorised f0 -> MIDI. Non-positive frequencies come back as NaN."""
    f0 = np.asarray(f0_hz, dtype=np.float64)
    out = np.full(f0.shape, np.nan)
    valid = f0 > 0
    out[valid] = A4_MIDI + 12.0 * np.log2(f0[valid] / A4_HZ)
    return out


def nan_median_filter(x: np.ndarray, window: int = MEDIAN_WINDOW) -> np.ndarray:
    """Sliding median that ignores NaN and never invents pitch where there was none.

    scipy.signal.medfilt would propagate NaN across the whole window and would
    also happily fill a gated frame with a neighbour's pitch. Both are wrong
    here: gated frames must stay gated.
    """
    x = np.asarray(x, dtype=np.float64)
    if window < 3:
        return x.copy()
    if window % 2 == 0:
        window += 1
    half = window // 2
    n = x.shape[0]
    out = np.full(n, np.nan)
    for i in range(n):
        if np.isnan(x[i]):
            continue
        lo, hi = max(0, i - half), min(n, i + half + 1)
        w = x[lo:hi]
        w = w[~np.isnan(w)]
        if w.size:
            out[i] = float(np.median(w))
    return out


def quantize_with_hysteresis(
    midi: np.ndarray,
    hysteresis_st: float = HYSTERESIS_ST,
    switch_frames: int = SWITCH_FRAMES,
) -> np.ndarray:
    """Round to semitones, but stick to the current note inside a hysteresis band.

    A plain round() splits one vibrato'd note into three whenever the pitch
    wanders across a rounding boundary. Holding a band of +/-(0.5 + hysteresis/2)
    around the *current* semitone means vibrato stays one note while a genuine
    half-step move (~1.0 semitone away) still switches.

    `switch_frames` debounces the switch: a new pitch has to hold for that many
    frames before it counts. The frames spent deciding are backfilled so the
    onset timestamp stays honest.
    """
    midi = np.asarray(midi, dtype=np.float64)
    band = 0.5 + hysteresis_st / 2.0
    out = np.full(midi.shape, np.nan)

    current: Optional[float] = None
    pending: Optional[float] = None
    pending_n = 0

    for i, m in enumerate(midi):
        if np.isnan(m):
            current = None
            pending, pending_n = None, 0
            continue

        if current is None:
            current = float(np.round(m))
            pending, pending_n = None, 0
        elif abs(m - current) <= band:
            pending, pending_n = None, 0
        else:
            candidate = float(np.round(m))
            if pending is not None and candidate == pending:
                pending_n += 1
            else:
                pending, pending_n = candidate, 1
            if pending_n >= max(1, switch_frames):
                current = pending
                # Backfill the frames we spent deciding so the onset isn't late.
                for j in range(i - pending_n + 1, i):
                    if j >= 0 and not np.isnan(out[j]):
                        out[j] = current
                pending, pending_n = None, 0

        out[i] = current

    return out


def segment_notes(
    f0_hz: Sequence[float],
    periodicity: Sequence[float],
    hop_s: float = 0.01,
    conf_threshold: float = 0.5,
    median_window: int = MEDIAN_WINDOW,
    hysteresis_st: float = HYSTERESIS_ST,
    switch_frames: int = SWITCH_FRAMES,
    min_note_s: float = MIN_NOTE_S,
    max_gap_s: float = MAX_GAP_S,
    midi_range: tuple = (24, 72),
) -> List[dict]:
    """Per-frame (f0, periodicity) -> list of {onset, duration, midi, confidence}."""
    f0 = np.asarray(f0_hz, dtype=np.float64).ravel()
    per = np.asarray(periodicity, dtype=np.float64).ravel()
    if f0.size == 0:
        return []
    if per.size != f0.size:
        raise ValueError(f"f0 ({f0.size}) and periodicity ({per.size}) length mismatch")

    # 1. gate on confidence
    midi = hz_to_midi(f0)
    midi[per < conf_threshold] = np.nan

    # bass fundamentals only; anything outside is a harmonic or a leak from
    # another stem, and it is cheaper to drop it here than to explain it later.
    lo, hi = midi_range
    midi[(midi < lo) | (midi > hi)] = np.nan

    # 3. median filter, then 4. quantize with hysteresis
    midi = nan_median_filter(midi, median_window)
    quantized = quantize_with_hysteresis(midi, hysteresis_st, switch_frames)

    # 5. group runs into notes
    max_gap_frames = int(round(max_gap_s / hop_s))
    notes: List[dict] = []
    run_pitch: Optional[int] = None
    run_start = 0
    run_end = 0  # exclusive, last voiced frame + 1
    run_conf: List[float] = []

    def flush() -> None:
        if run_pitch is None:
            return
        duration = (run_end - run_start) * hop_s
        if duration < min_note_s:
            return
        notes.append(
            {
                "onset": round(run_start * hop_s, 4),
                "duration": round(duration, 4),
                "midi": int(run_pitch),
                "confidence": round(float(np.mean(run_conf)) if run_conf else 0.0, 3),
            }
        )

    for i, q in enumerate(quantized):
        if np.isnan(q):
            # Tolerate a short dropout without ending the note.
            if run_pitch is not None and (i - run_end) >= max_gap_frames:
                flush()
                run_pitch, run_conf = None, []
            continue

        pitch = int(q)
        if run_pitch is None:
            run_pitch, run_start, run_conf = pitch, i, []
        elif pitch != run_pitch or (i - run_end) > max_gap_frames:
            flush()
            run_pitch, run_start, run_conf = pitch, i, []

        run_end = i + 1
        run_conf.append(float(per[i]))

    flush()
    return notes


def estimate_tempo(
    onsets: Iterable[float],
    min_bpm: float = 60.0,
    max_bpm: float = 180.0,
    bin_s: float = 0.02,
) -> Optional[float]:
    """Mode of the inter-onset-interval histogram, octave-folded into range.

    Deliberately cheap. It is good enough to print next to the tab and it is not
    worth more than this until something downstream actually consumes it.
    """
    times = np.asarray(sorted(onsets), dtype=np.float64)
    if times.size < 5:
        return None

    iois = np.diff(times)
    iois = iois[(iois >= 0.12) & (iois <= 2.0)]
    if iois.size < 4:
        return None

    bins = np.arange(0.12, 2.0 + bin_s, bin_s)
    hist, edges = np.histogram(iois, bins=bins)
    if hist.max() < 2:
        return None
    period = float((edges[hist.argmax()] + edges[hist.argmax() + 1]) / 2.0)
    if period <= 0:
        return None

    bpm = 60.0 / period
    for _ in range(4):
        if bpm < min_bpm:
            bpm *= 2.0
        elif bpm > max_bpm:
            bpm /= 2.0
        else:
            break
    return round(bpm, 1)
