import base64
import os
import sys
import tempfile
import time
import urllib.request

import runpod
import torch

from transcribe import pitch_to_notes, separate_bass, stem_to_wav_bytes

# Stamped at import, i.e. as soon as the container has a Python process. The gap
# between this and the first request's t0 is the part of cold start that is
# container pull + interpreter boot, which is the number Phase 2 wants split out.
WORKER_BOOT_TS = time.time()

MODEL = None
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


def _log(msg: str) -> None:
    print(f"[{time.time() - WORKER_BOOT_TS:7.2f}s since boot] {msg}", file=sys.stderr, flush=True)


def _load():
    """Load once per worker, not once per request.

    This is most of the difference between a good cold-start number and a bad
    one. Module-level lazy load means the weights land in GPU memory on the
    first request a worker sees and stay resident for every request after it.
    """
    global MODEL
    if MODEL is None:
        t = time.time()
        from demucs.pretrained import get_model

        MODEL = get_model("htdemucs").to(DEVICE).eval()
        _log(f"htdemucs loaded onto {DEVICE} in {time.time() - t:.2f}s")
    return MODEL


def _fetch_audio(inp: dict) -> bytes:
    if inp.get("audio_b64"):
        return base64.b64decode(inp["audio_b64"])
    if inp.get("audio_url"):
        url = inp["audio_url"]
        if not url.startswith(("http://", "https://")):
            raise ValueError("audio_url must be http(s)")
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.read(MAX_DOWNLOAD_BYTES + 1)
    raise ValueError("provide audio_b64 or audio_url")


def handler(job):
    t0 = time.time()
    inp = job.get("input") or {}
    cold = MODEL is None

    try:
        raw = _fetch_audio(inp)
    except Exception as exc:
        return {"error": str(exc)}
    if len(raw) > MAX_DOWNLOAD_BYTES:
        return {"error": f"audio exceeds {MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB"}

    with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as f:
        f.write(raw)
        path = f.name

    try:
        t_load = time.time()
        model = _load()
        t1 = time.time()

        stem, sr = separate_bass(
            model, path, DEVICE, max_s=inp.get("max_duration_s", 300)
        )
        t2 = time.time()

        notes, tempo = pitch_to_notes(
            stem,
            sr,
            DEVICE,
            conf_threshold=inp.get("confidence_threshold", 0.5),
        )
        t3 = time.time()

        out = {
            "notes": notes,
            "tempo_bpm_estimate": tempo,
            "duration_s": round(len(stem) / sr, 2),
            "timings": {
                "fetch_s": round(t_load - t0, 2),
                "model_load_s": round(t1 - t_load, 2),
                "separation_s": round(t2 - t1, 2),
                "pitch_s": round(t3 - t2, 2),
                "total_s": round(t3 - t0, 2),
                "worker_uptime_at_request_s": round(t0 - WORKER_BOOT_TS, 2),
                "cold": cold,
            },
        }
        if inp.get("return_stem"):
            out["stem_wav_b64"] = base64.b64encode(stem_to_wav_bytes(stem, sr)).decode()

        _log(
            f"job done: {len(notes)} notes, sep={out['timings']['separation_s']}s "
            f"pitch={out['timings']['pitch_s']}s cold={cold}"
        )
        return out
    except Exception as exc:  # a traceback in the logs beats a silent 500
        import traceback

        traceback.print_exc()
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        os.unlink(path)


_log(f"worker up, device={DEVICE}, TORCH_HOME={os.environ.get('TORCH_HOME', '<default>')}")

runpod.serverless.start({"handler": handler})
