#!/usr/bin/env python3
"""Send the synthetic test tone to a live endpoint and check what comes back.

This is the self-verifying half of make_test_tone.py: because the input pitches
are known exactly, the endpoint's output can be asserted rather than eyeballed.
A pass proves separation, pitch tracking and segmentation all work end to end on
real hardware.

  python tools/verify_endpoint.py --audio test_tone.wav

Reads RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID from the environment (or .env).
Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_EXPECTED = [28, 33, 38, 43]  # open strings: E1 A1 D2 G2
ONSET_TOLERANCE_S = 0.15


def load_env(path: Path) -> None:
    """Minimal .env loader so the key never has to be exported by hand."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}


def await_job(payload: dict, key: str, endpoint: str, poll_s: float = 3.0,
              timeout_s: float = 900) -> dict:
    """/runsync returns IN_QUEUE once its bounded wait expires; poll to the end.

    A cold start on this endpoint runs ~220s, far past that window, so without
    polling every cold verification would report a spurious failure.
    """
    status = payload.get("status")
    if status in TERMINAL or status is None or not payload.get("id"):
        return payload

    job_id = payload["id"]
    deadline = time.time() + timeout_s
    waited = 0.0
    while time.time() < deadline:
        time.sleep(poll_s)
        waited += poll_s
        req = urllib.request.Request(
            f"https://api.runpod.ai/v2/{endpoint}/status/{job_id}",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read())
        state = payload.get("status")
        print(f"  [{waited:5.0f}s] {state}", flush=True)
        if state in TERMINAL:
            return payload
    return {"status": "CLIENT_TIMEOUT", "id": job_id}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--audio", default="test_tone.wav")
    p.add_argument("--expected", default=",".join(str(n) for n in DEFAULT_EXPECTED))
    p.add_argument("--conf", type=float, default=0.5)
    p.add_argument("--note-s", type=float, default=0.8, help="expected spacing between onsets")
    a = p.parse_args()

    load_env(Path(__file__).resolve().parent.parent / ".env")
    key = os.environ.get("RUNPOD_API_KEY")
    endpoint = os.environ.get("RUNPOD_ENDPOINT_ID")
    if not (key and endpoint):
        sys.exit("set RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID (or fill in .env)")

    expected = [int(x) for x in a.expected.split(",") if x.strip()]
    raw = Path(a.audio).read_bytes()
    print(f"sending {len(raw)/1e6:.2f}MB to endpoint {endpoint}")

    body = json.dumps(
        {
            "input": {
                "audio_b64": base64.b64encode(raw).decode(),
                "max_duration_s": 30,
                "confidence_threshold": a.conf,
            }
        }
    ).encode()

    req = urllib.request.Request(
        f"https://api.runpod.ai/v2/{endpoint}/runsync",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            payload = json.loads(resp.read())
        payload = await_job(payload, key, endpoint)
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read()[:500].decode(errors='replace')}", file=sys.stderr)
        return 2
    wall = time.perf_counter() - t0

    if payload.get("status") not in (None, "COMPLETED"):
        print(f"job status {payload.get('status')}: {json.dumps(payload)[:500]}", file=sys.stderr)
        return 2

    out = payload.get("output") or {}
    if "error" in out:
        print(f"worker error: {out['error']}", file=sys.stderr)
        return 2

    notes = out.get("notes", [])
    timings = out.get("timings", {})

    print(f"\nwall {wall:.1f}s  |  delayTime {payload.get('delayTime')}ms  "
          f"executionTime {payload.get('executionTime')}ms")
    print(f"worker: {json.dumps(timings)}")
    print(f"\n{len(notes)} notes returned:")
    for n in notes:
        print(f"  onset {n['onset']:6.3f}s  dur {n['duration']:5.3f}s  "
              f"midi {n['midi']:3d}  conf {n['confidence']:.2f}")

    # --- assertions -------------------------------------------------------
    got = [n["midi"] for n in notes]
    ok = True

    if got == expected:
        print(f"\nPASS  pitches exactly match {expected}")
    else:
        ok = False
        print(f"\nFAIL  pitches {got} != expected {expected}")
        octave = [g - e for g, e in zip(got, expected) if abs(g - e) == 12]
        if octave:
            print(f"      {len(octave)} look like octave errors -- tune median_window in segment.py")

    if len(notes) == len(expected):
        drift = [
            abs(n["onset"] - i * a.note_s) for i, n in enumerate(notes)
        ]
        worst = max(drift) if drift else 0.0
        if worst <= ONSET_TOLERANCE_S:
            print(f"PASS  onsets within {worst*1000:.0f}ms of expected grid")
        else:
            ok = False
            print(f"FAIL  worst onset drift {worst*1000:.0f}ms exceeds {ONSET_TOLERANCE_S*1000:.0f}ms")

    print("\n" + ("VERIFIED end to end" if ok else "output did not match expectations"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
