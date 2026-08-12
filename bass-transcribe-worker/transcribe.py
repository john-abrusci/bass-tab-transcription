"""Stage 1 (separation) + stage 2 (pitch tracking).

Both stages live in one process on purpose: the bass stem never leaves memory,
so there is no second cold start and no stem round-trip over the network. The
price is a bigger image, which is a trade worth making when the metric we care
about is end-to-end latency.
"""

from __future__ import annotations

import io
from math import gcd
from typing import List, Optional, Tuple

import numpy as np
import torch
from scipy.signal import resample_poly

from segment import estimate_tempo, segment_notes

# torchcrepe is trained at 16kHz and only accepts 16kHz input.
CREPE_SR = 16_000
CREPE_HOP = 160  # samples @ 16kHz = 10ms frames
CREPE_MODEL = "full"

# Low E on a 4-string is ~41Hz; a 5-string low B is ~31Hz. Give the tracker a
# little headroom below and cap it well under where a bass fundamental can go,
# so it cannot lock onto a harmonic and report the octave above.
BASS_FMIN = 28.0
BASS_FMAX = 500.0


def separate_bass(
    model,
    path: str,
    device: torch.device,
    max_s: Optional[float] = 300,
) -> Tuple[np.ndarray, int]:
    """Run htdemucs and return (mono bass stem as float32, sample_rate)."""
    from demucs.apply import apply_model
    from demucs.audio import AudioFile

    wav = AudioFile(path).read(
        streams=0,
        samplerate=model.samplerate,
        channels=model.audio_channels,
    )
    if max_s:
        wav = wav[:, : int(max_s * model.samplerate)]

    # Demucs expects input normalised against the mixture's own statistics.
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std()
    wav = (wav - mean) / (std + 1e-8)

    with torch.no_grad():
        sources = apply_model(
            model,
            wav[None].to(device),
            device=device,
            split=True,
            overlap=0.25,
            progress=False,
        )[0]
    sources = sources * (std + 1e-8) + mean

    bass = sources[model.sources.index("bass")]
    stem = bass.mean(0).cpu().numpy().astype(np.float32)  # downmix to mono
    return stem, int(model.samplerate)


def _to_crepe_rate(stem: np.ndarray, sr: int) -> np.ndarray:
    if sr == CREPE_SR:
        return stem
    g = gcd(int(sr), CREPE_SR)
    return resample_poly(stem, CREPE_SR // g, sr // g).astype(np.float32)


def pitch_to_notes(
    stem: np.ndarray,
    sr: int,
    device: torch.device,
    conf_threshold: float = 0.5,
    **segment_kwargs,
) -> Tuple[List[dict], Optional[float]]:
    """Bass stem -> (note events, tempo estimate)."""
    import torchcrepe

    audio = _to_crepe_rate(stem, sr)
    peak = float(np.abs(audio).max())
    if peak > 0:
        audio = audio / peak  # crepe is happier with a normalised input

    tensor = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None]

    f0, periodicity = torchcrepe.predict(
        tensor,
        CREPE_SR,
        hop_length=CREPE_HOP,
        fmin=BASS_FMIN,
        fmax=BASS_FMAX,
        model=CREPE_MODEL,
        batch_size=512,
        device=str(device),
        return_periodicity=True,
    )

    # A 3-frame median on periodicity alone stops single-frame confidence dips
    # from punching holes in otherwise solid notes. The f0 track is left raw --
    # segment.py medians it in MIDI space, which is the domain where octave
    # errors are a constant offset rather than a factor of two.
    periodicity = torchcrepe.filter.median(periodicity, 3)

    f0_np = f0.squeeze(0).detach().cpu().numpy()
    per_np = periodicity.squeeze(0).detach().cpu().numpy()

    notes = segment_notes(
        f0_np,
        per_np,
        hop_s=CREPE_HOP / CREPE_SR,
        conf_threshold=conf_threshold,
        **segment_kwargs,
    )
    tempo = estimate_tempo([n["onset"] for n in notes])
    return notes, tempo


def stem_to_wav_bytes(stem: np.ndarray, sr: int) -> bytes:
    """Encode the isolated stem as a wav, for `return_stem: true` / debugging."""
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, stem, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()
