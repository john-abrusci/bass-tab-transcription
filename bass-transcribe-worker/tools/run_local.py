#!/usr/bin/env python3
"""Run the full transcription pipeline locally, without a GPU endpoint.

Imports the same `transcribe.py` and `segment.py` the worker runs, so the notes
produced here are the notes the endpoint would produce. Device affects latency,
not which notes come out -- which means accuracy evaluation needs no deployed
infrastructure, no API key and no spend, and stays reproducible in CI.

Do NOT use this for timing. Every number in PHASE2.md came from real hardware
through bench/measure.py, and a local CPU/MPS run is not comparable.

  python tools/run_local.py track.wav --out notes.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from transcribe import pitch_to_notes, separate_bass  # noqa: E402
from _audio import load_audio  # noqa: E402


def pick_device(requested: str) -> torch.device:
    if requested != "auto":
        return torch.device(requested)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("audio")
    p.add_argument("--out", default="notes.json")
    p.add_argument("--max-s", type=float, default=300)
    p.add_argument("--conf", type=float, default=0.5)
    p.add_argument("--device", default="auto")
    a = p.parse_args()

    from demucs.pretrained import get_model

    device = pick_device(a.device)
    print(f"device: {device}")

    model = get_model("htdemucs").to(device).eval()
    # Decode here rather than in separate_bass, which would need ffmpeg.
    wav = load_audio(a.audio, model.samplerate, model.audio_channels)

    t0 = time.time()
    try:
        stem, sr = separate_bass(model, a.audio, device, max_s=a.max_s, wav=wav)
    except (NotImplementedError, RuntimeError) as exc:
        if device.type != "mps":
            raise
        print(f"MPS failed ({type(exc).__name__}); falling back to CPU")
        device = torch.device("cpu")
        model = model.to(device)
        stem, sr = separate_bass(model, a.audio, device, max_s=a.max_s, wav=wav)
    t1 = time.time()

    # torchcrepe is CPU/CUDA only in practice; MPS support is unreliable.
    pitch_device = device if device.type == "cuda" else torch.device("cpu")
    notes, tempo = pitch_to_notes(stem, sr, pitch_device, conf_threshold=a.conf)
    t2 = time.time()

    out = {
        "notes": notes,
        "tempo_bpm_estimate": tempo,
        "duration_s": round(len(stem) / sr, 2),
        "local_timings_not_comparable_to_phase2": {
            "separation_s": round(t1 - t0, 2),
            "pitch_s": round(t2 - t1, 2),
        },
    }
    Path(a.out).write_text(json.dumps(out, indent=2))

    pitches = [n["midi"] for n in notes]
    print(f"{len(notes)} notes, tempo {tempo}, {out['duration_s']}s audio")
    if pitches:
        print(f"midi range {min(pitches)}-{max(pitches)}")
    print(f"wrote {a.out}  (separation {t1-t0:.1f}s, pitch {t2-t1:.1f}s -- local, not Phase 2 numbers)")


if __name__ == "__main__":
    main()
