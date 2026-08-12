"""Audio loading for the local tools.

The worker runs inside a container with ffmpeg installed, so `transcribe.py` uses
Demucs' own `AudioFile` and decodes anything. The local tools cannot assume
ffmpeg is present, so they try soundfile first -- which covers wav, flac and ogg
without any system dependency -- and fall back to `AudioFile` when it isn't
enough. Deliberately kept out of `transcribe.py`: the deployed path should stay
exactly what the container runs.
"""

from __future__ import annotations

from math import gcd

import numpy as np
import torch


def load_audio(path: str, samplerate: int, channels: int) -> torch.Tensor:
    """Load to a (channels, samples) float32 tensor at `samplerate`."""
    try:
        import soundfile as sf

        data, sr = sf.read(path, dtype="float32", always_2d=True)  # (samples, ch)
    except Exception:
        from demucs.audio import AudioFile

        return AudioFile(path).read(streams=0, samplerate=samplerate, channels=channels)

    wav = data.T  # (ch, samples)

    if sr != samplerate:
        from scipy.signal import resample_poly

        g = gcd(int(sr), int(samplerate))
        wav = resample_poly(wav, samplerate // g, sr // g, axis=1).astype(np.float32)

    if wav.shape[0] > channels:
        wav = wav.mean(0, keepdims=True).repeat(channels, axis=0)
    elif wav.shape[0] < channels:
        wav = np.repeat(wav, channels // wav.shape[0], axis=0)

    return torch.from_numpy(np.ascontiguousarray(wav, dtype=np.float32))
