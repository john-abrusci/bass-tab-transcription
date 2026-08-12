#!/usr/bin/env python3
"""Produce a bass-free backing track from any recording.

Why this exists: the eval fixture needs a bassline whose onsets and pitches are
known exactly, mixed under audio that gives separation a realistic problem. You
cannot simply mix a synthetic bass under a song, because the song already has
bass -- Demucs would separate both and the ground truth would be meaningless.

So: separate the track, throw the bass stem away, and keep drums + other +
vocals. Mix the synthetic bass under that and separation faces real recorded
instruments while every ground-truth note stays exact.

  python tools/strip_bass.py song.mp3 --out backing.wav --max-s 120

Runs locally on CPU or MPS. No GPU endpoint required -- separation quality does
not depend on which device runs it.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))


def pick_device(requested: str) -> torch.device:
    if requested != "auto":
        return torch.device(requested)
    if torch.cuda.is_available():
        return torch.device("cuda")
    # MPS is markedly faster than CPU here, but Demucs has historically hit
    # unimplemented-op errors on it; fall back rather than fail.
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("audio")
    p.add_argument("--out", default="backing.wav")
    p.add_argument("--max-s", type=float, default=None, help="truncate input")
    p.add_argument("--device", default="auto")
    p.add_argument("--keep", default="drums,other,vocals",
                   help="stems to keep in the backing")
    a = p.parse_args()

    from demucs.apply import apply_model
    from demucs.pretrained import get_model
    import soundfile as sf

    from _audio import load_audio

    device = pick_device(a.device)
    print(f"device: {device}")

    model = get_model("htdemucs").to(device).eval()
    wav = load_audio(a.audio, model.samplerate, model.audio_channels)
    if a.max_s:
        wav = wav[:, : int(a.max_s * model.samplerate)]
    print(f"input: {wav.shape[1]/model.samplerate:.1f}s")

    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std()
    wav_n = (wav - mean) / (std + 1e-8)

    t0 = time.time()
    with torch.no_grad():
        try:
            sources = apply_model(model, wav_n[None].to(device), device=device,
                                  split=True, overlap=0.25, progress=False)[0]
        except (NotImplementedError, RuntimeError) as exc:
            if device.type != "mps":
                raise
            print(f"MPS failed ({type(exc).__name__}); falling back to CPU")
            device = torch.device("cpu")
            model = model.to(device)
            sources = apply_model(model, wav_n[None].to(device), device=device,
                                  split=True, overlap=0.25, progress=False)[0]
    sources = sources * (std + 1e-8) + mean
    print(f"separated in {time.time()-t0:.1f}s")

    keep = [s.strip() for s in a.keep.split(",")]
    idx = [model.sources.index(s) for s in keep]
    backing = sources[idx].sum(0)  # (channels, samples)

    # Report what was discarded, as a sanity check that the bass stem was real.
    bass = sources[model.sources.index("bass")]
    rms = lambda x: float(torch.sqrt((x.float() ** 2).mean()))
    print(f"kept {keep}: rms {rms(backing):.4f}")
    print(f"discarded bass:  rms {rms(bass):.4f}")

    out = backing.cpu().numpy().T.astype(np.float32)
    peak = np.abs(out).max()
    if peak > 0:
        out = out / peak * 0.89
    sf.write(a.out, out, model.samplerate)
    print(f"wrote {a.out}: {out.shape[0]/model.samplerate:.1f}s, "
          f"{Path(a.out).stat().st_size/1e6:.2f}MB")


if __name__ == "__main__":
    main()
