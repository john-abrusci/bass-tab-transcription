#!/usr/bin/env python3
"""Write a test_input.json for local `runpod` testing or a raw /runsync body.

  python tools/encode.py clip.wav                 # -> test_input.json
  python tools/encode.py clip.wav --stdout        # -> full request body on stdout
"""

import argparse
import base64
import json
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("audio")
    p.add_argument("--out", default="test_input.json")
    p.add_argument("--max-s", type=int, default=30)
    p.add_argument("--conf", type=float, default=0.5)
    p.add_argument("--stdout", action="store_true")
    a = p.parse_args()

    raw = Path(a.audio).read_bytes()
    body = {
        "input": {
            "audio_b64": base64.b64encode(raw).decode(),
            "max_duration_s": a.max_s,
            "confidence_threshold": a.conf,
            "return_stem": False,
        }
    }
    if a.stdout:
        print(json.dumps(body))
    else:
        Path(a.out).write_text(json.dumps(body))
        mb = len(raw) / 1e6
        print(f"{a.out}: {mb:.2f}MB raw -> {mb * 4 / 3:.2f}MB base64")


if __name__ == "__main__":
    main()
