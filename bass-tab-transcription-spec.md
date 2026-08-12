# Bass Tab Transcription — Build Spec

**Goal:** Upload an audio file, get back a bass tab. GPU inference runs on Runpod Serverless; the app layer stays where it already is (Next.js).

**Primary objective (be honest about this):** Phases 0–2 exist to give you first-hand experience with Runpod Serverless and a set of real numbers — cold start, warm execution, cost per job, GPU choice rationale. Phase 3+ makes it a real product. If time is short, ship Phase 0–2 and stop.

---

## Architecture

```
Next.js app (existing)
  │
  │  POST audio → /runsync
  ▼
Runpod Serverless endpoint
  ├─ Stage 1: Demucs (htdemucs) → isolated bass stem
  └─ Stage 2: torchcrepe → f0 track → note events
  │
  │  ← JSON note list
  ▼
Next.js app
  ├─ Stage 3: fretboard assignment (DP over hand position)  [Phase 3]
  └─ Stage 4: render in existing tab viewer                  [Phase 4]
```

**Design decision worth stating up front:** stages 1 and 2 live in one endpoint, not two. Two endpoints means two cold starts and a stem round-trip over the network. One endpoint keeps the stem in memory. The cost is a larger image. This is the kind of tradeoff to be ready to defend.

---

## Model choices

**Source separation: `htdemucs`** (Hybrid Transformer Demucs). It's the current default in the `demucs` package, PyTorch-native, and separates into drums/bass/other/vocals. You want the `bass` stem.

**Pitch tracking: `torchcrepe`** over Spotify's `basic-pitch`. Reasoning:

| | torchcrepe | basic-pitch |
|---|---|---|
| Runtime | PyTorch (same as Demucs) | TensorFlow (second runtime) |
| Image size impact | Minimal | +~2GB |
| Output | Per-frame f0 + periodicity | Note events directly |
| Fit for bass | Monophonic, good low-freq | Polyphonic-general |
| Work you must do | Note segmentation | None |

torchcrepe means one runtime, a leaner image, and a faster cold start — which is the metric this whole exercise is about. The cost is that you write note segmentation yourself. That's ~40 lines and it's tunable, which is an advantage for an instrument where you care about note boundaries.

Fall back to `basic-pitch` if torchcrepe's low-frequency accuracy disappoints on your test set. Bass fundamentals run ~41 Hz (low E) to ~400 Hz, and pitch trackers get less reliable at the bottom of that range.

---

## Repo structure

```
bass-transcribe-worker/
├── Dockerfile
├── requirements.txt
├── handler.py
├── transcribe.py        # separation + pitch → notes
├── segment.py           # f0 frames → note events
└── test_input.json      # local testing
```

---

## Dockerfile

```dockerfile
FROM pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake model weights into the image so cold starts don't download them.
# Phase 2: try moving these to a network volume and compare cold start.
RUN python -c "from demucs.pretrained import get_model; get_model('htdemucs')"
RUN python -c "import torchcrepe, torch; torchcrepe.load.model(torch.device('cpu'), 'full')"

COPY . .

CMD ["python", "-u", "handler.py"]
```

`ffmpeg` is required — Demucs uses it to decode anything that isn't a wav.

## requirements.txt

```
runpod
demucs
torchcrepe
soundfile
numpy
scipy
```

---

## Endpoint contract

**Request** — `POST https://api.runpod.ai/v2/{ENDPOINT_ID}/runsync`

```json
{
  "input": {
    "audio_b64": "<base64 mp3/wav>",
    "audio_url": "https://... (alternative to audio_b64)",
    "max_duration_s": 300,
    "confidence_threshold": 0.5,
    "return_stem": false
  }
}
```

Accept either `audio_b64` or `audio_url`. Base64 is simpler to start; a 4-minute mp3 is ~4MB, which inflates to ~5.3MB encoded and is within request limits but wasteful. **Phase 2 decision point:** measure whether payload transfer is a meaningful share of total latency. If it is, switch to presigned-URL upload and pass the URL.

**Response**

```json
{
  "output": {
    "notes": [
      { "onset": 0.512, "duration": 0.244, "midi": 40, "confidence": 0.93 }
    ],
    "tempo_bpm_estimate": 118.0,
    "duration_s": 213.4,
    "timings": {
      "separation_s": 14.2,
      "pitch_s": 3.1,
      "total_s": 17.9
    }
  }
}
```

**Return `timings` in the response.** This is not decoration — it's how you get the numbers for Phase 2 without instrumenting separately.

---

## handler.py

```python
import base64, io, os, time, tempfile
import runpod
import torch
from transcribe import separate_bass, pitch_to_notes

MODEL = None
CREPE_DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

def _load():
    """Load once per worker, not once per request."""
    global MODEL
    if MODEL is None:
        from demucs.pretrained import get_model
        MODEL = get_model("htdemucs").to(CREPE_DEVICE).eval()
    return MODEL

def handler(job):
    t0 = time.time()
    inp = job["input"]

    # --- resolve audio to a temp file ---
    if "audio_b64" in inp:
        raw = base64.b64decode(inp["audio_b64"])
    elif "audio_url" in inp:
        import urllib.request
        raw = urllib.request.urlopen(inp["audio_url"]).read()
    else:
        return {"error": "provide audio_b64 or audio_url"}

    with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as f:
        f.write(raw)
        path = f.name

    try:
        model = _load()

        t1 = time.time()
        stem, sr = separate_bass(model, path, CREPE_DEVICE,
                                 max_s=inp.get("max_duration_s", 300))
        t2 = time.time()

        notes, tempo = pitch_to_notes(
            stem, sr, CREPE_DEVICE,
            conf_threshold=inp.get("confidence_threshold", 0.5),
        )
        t3 = time.time()

        return {
            "notes": notes,
            "tempo_bpm_estimate": tempo,
            "duration_s": len(stem) / sr,
            "timings": {
                "separation_s": round(t2 - t1, 2),
                "pitch_s": round(t3 - t2, 2),
                "total_s": round(t3 - t0, 2),
            },
        }
    finally:
        os.unlink(path)

runpod.serverless.start({"handler": handler})
```

**The `_load()` pattern matters.** Module-level lazy load means the model loads once per *worker*, then stays resident for subsequent requests on that worker. Loading inside the request path would make every call pay model-load cost. This single detail is most of the difference between a good cold-start number and a bad one.

---

## Note segmentation (segment.py)

torchcrepe gives you per-frame `(f0_hz, periodicity)` at ~10ms hop. Turning that into notes:

1. **Gate on confidence** — drop frames where `periodicity < threshold`. These are silence or unpitched noise.
2. **Convert to MIDI** — `midi = 69 + 12 * log2(f0 / 440)`.
3. **Median filter** — a 5-frame median over MIDI kills octave-jump errors, which are the characteristic CREPE failure on bass.
4. **Quantize to semitones** — round, but hold a hysteresis band (~0.6 semitone) so vibrato doesn't split one note into three.
5. **Group runs** — consecutive frames at the same semitone become one note event. Drop anything shorter than ~60ms as noise.
6. **Tempo estimate** — cheap version: histogram inter-onset intervals and take the mode. Good enough to display; don't over-invest.

Steps 3 and 4 are where output quality actually lives. Expect to tune them against real songs.

---

## Deploying

1. Build and push: `docker build -t <user>/bass-transcribe:v1 . && docker push <user>/bass-transcribe:v1`
2. In the Runpod console, create a **Serverless endpoint** from that image.
3. Configure: GPU type, max workers, idle timeout, and (initially) **min workers = 0** so you feel the real cold start.
4. Test with `/runsync` and a small wav before trying a full song.

**Verify the SDK and endpoint details against current docs** — `runpod.serverless.start`, the `/run` vs `/runsync` split, and console configuration options are all stable in outline but the specifics move. Don't let a stale detail from this spec cost you an hour.

---

## Phase 2: the measurement checklist

This is the deliverable that matters for the interview. Record all of it.

**Cold start**
- [ ] Time from request to first byte with zero warm workers
- [ ] Split: container pull vs. model load vs. inference (log timestamps at worker start)
- [ ] Repeat with weights on a **network volume** instead of baked in — which is faster, and by how much?

**Warm execution**
- [ ] Separation time for a 3–4 min track
- [ ] Pitch tracking time
- [ ] Total round-trip including payload transfer

**GPU selection** — run the same 4-minute track on three tiers and record time and cost:

| GPU | Separation time | Cost/hr | Cost/job | Notes |
|---|---|---|---|---|
| RTX A4000 / A5000 | | | | cheapest viable |
| RTX 4090 / L40S | | | | mid |
| A100 / H100 | | | | almost certainly overkill |

Then answer the question that makes this a product exercise rather than a hobby: **at what job volume does a faster, pricier GPU become the cheaper choice per job?** For a bursty consumer workload the answer is usually "never" — but derive it rather than assuming it.

**Scale-to-zero vs. warm**
- [ ] Cost of one always-warm worker per day at idle
- [ ] Number of jobs/day at which a warm worker pays for itself in latency
- [ ] Your actual opinion on which you'd ship, and why

**Concurrency**
- [ ] Fire 10 simultaneous requests. What happens to p50 and p95?
- [ ] Does scale-up keep pace, or do requests queue?

---

## Phase 3: fretboard assignment

Pure app-layer logic, no ML. Each MIDI pitch maps to several string/fret pairs on a 4-string bass in standard tuning (E1/A1/D2/G2 = MIDI 28/33/38/43). Pick the sequence a human would actually play.

Dynamic programming over note events:
- **State:** (string, fret) for the current note
- **Cost:** fret-distance from previous position + string-change penalty + open-string bonus + penalty for frets above ~12 + penalty for leaving a comfortable 4-fret hand span
- **Objective:** minimize total path cost

Weights are taste. Tune them against tabs you know are right.

---

## Phase 4: evaluation harness

Repurpose the Ultimate Guitar tabs you already scraped as **ground truth for evaluation only** — not as product content. Pick 20 songs with well-reviewed human tabs and measure:

- **Note-level F1** — onset within ±50ms and correct pitch counts as a match
- **Pitch accuracy** ignoring timing
- **Position accuracy** — does your DP pick the same string/fret a human chose?

Track this as you tune segmentation and the DP weights. "I built an eval harness and moved note-level F1 from 0.61 to 0.78 by fixing octave errors in the median filter" is a substantially better interview sentence than anything about the product itself.

---

## Legal note

Keep this personal, on music you own. Private-use transcription is fine; running it as a service for arbitrary uploads is a different question. Don't ship the scraper as a content source.
