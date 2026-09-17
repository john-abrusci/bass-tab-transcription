#!/usr/bin/env python3
"""Downsample the synthetic test tone to mono 16 kHz.

Phase 3 lost five trials to `Broken pipe` and `SSL_ALERT_BAD_RECORD_MAC` on the
base64 upload path -- the transport faults Phase 2 documented. Shrinking the
request body from 735 KB to 133 KB took the failure rate from 5-of-9 to 0-of-10,
which is what made a 10-trial run possible at all.

The audio is unchanged in duration and content: same four open-string notes, same
3.2s. Only the sample rate and channel count drop, which is ample for bass
fundamentals (41-98 Hz) and for torchcrepe, which resamples to 16 kHz internally
anyway.

  python tools/make_test_tone.py            # -> test_tone.wav      (551 KB)
  python tools/downsample_tone.py           # -> test_tone_16k.wav  (100 KB)

Expected sha256 of the output, as recorded on every Phase 3 round-2 row:
  9e0f18740e3ca6306b748f63f5c00a832b5dbc68793bf9c511f95ed2ee261d86

Needs numpy; the wav is read and written with the stdlib.
"""

from __future__ import annotations

import argparse
import hashlib
import wave
from pathlib import Path

import numpy as np

TARGET_SR = 16_000


def downsample(src: Path, dst: Path, target_sr: int = TARGET_SR) -> Path:
    with wave.open(str(src)) as w:
        n, sw, ch, sr = w.getnframes(), w.getsampwidth(), w.getnchannels(), w.getframerate()
        raw = w.readframes(n)

    if sw != 2:
        raise SystemExit(f"expected 16-bit input, got {sw * 8}-bit")

    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if ch == 2:
        a = a.reshape(-1, 2).mean(axis=1)

    # Linear interpolation is enough here: the source is a synthesised tone with
    # no content anywhere near the new Nyquist limit, so there is nothing to alias.
    idx = np.linspace(0, len(a) - 1, int(len(a) * target_sr / sr))
    a = np.interp(idx, np.arange(len(a)), a).astype(np.int16)

    with wave.open(str(dst), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(target_sr)
        out.writeframes(a.tobytes())
    return dst


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", default="test_tone.wav")
    p.add_argument("--dst", default="test_tone_16k.wav")
    p.add_argument("--sr", type=int, default=TARGET_SR)
    args = p.parse_args()

    src = Path(args.src)
    if not src.exists():
        raise SystemExit(f"{src} not found -- run tools/make_test_tone.py first")

    dst = downsample(src, Path(args.dst), args.sr)
    b = dst.read_bytes()
    with wave.open(str(dst)) as w:
        dur = w.getnframes() / w.getframerate()
    print(f"{dst}: {dur:.2f}s mono {args.sr} Hz, {len(b) / 1024:.0f} KB")
    print(f"sha256: {hashlib.sha256(b).hexdigest()}")


if __name__ == "__main__":
    main()
