# bass-transcribe-worker

Runpod Serverless worker: audio in, bass note events out. htdemucs for separation and
torchcrepe for pitch, both in one process so the stem never leaves memory.

## Test what you can without a GPU

Segmentation is the part you will actually tune, and it needs nothing but numpy and scipy:

```bash
python test_segment.py
```

Each test synthesises an f0 track with one specific pathology — vibrato, a two-frame
octave jump, a 20ms dropout — and asserts the notes come back out intact.

## Build and deploy

```bash
docker build -t <user>/bass-transcribe:v1 .
docker push <user>/bass-transcribe:v1
```

Then in the Runpod console, create a **Serverless endpoint** from that image and set:

- **GPU type** — start at the cheap tier (RTX A4000/A5000) and work up; Phase 2 is where
  you find out whether more is worth paying for.
- **Max workers** — enough for the concurrency test.
- **Idle timeout** — short, so you can reproduce a cold start without waiting.
- **Min workers = 0** initially, so you feel the real cold start rather than hiding it.
- **Execution timeout** — default is 10 minutes, comfortably above a 4-minute track.

Test with a short wav before a full song:

```bash
python tools/encode.py clip.wav --stdout > body.json
curl -X POST https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/runsync \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" -d @body.json
```

## Endpoint contract

Request:

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

Response:

```json
{
  "output": {
    "notes": [{ "onset": 0.512, "duration": 0.244, "midi": 40, "confidence": 0.93 }],
    "tempo_bpm_estimate": 118.0,
    "duration_s": 213.4,
    "timings": {
      "fetch_s": 0.3,
      "model_load_s": 0.0,
      "separation_s": 14.2,
      "pitch_s": 3.1,
      "total_s": 17.9,
      "worker_uptime_at_request_s": 412.0,
      "cold": false
    }
  }
}
```

`timings` is not decoration — it is how Phase 2 gets its numbers without a separate
instrumentation pass. `model_load_s` is non-zero exactly once per worker, and `cold` tells
you which request that was. `worker_uptime_at_request_s` separates container-pull time from
model-load time: it is how long the process had been alive when the request arrived.

Base64 is the simple path. A 4-minute mp3 is ~4MB raw, ~5.3MB encoded — within limits but
wasteful. Phase 2 decision point: if `round_trip - total_s` turns out to be a meaningful
share of latency, switch the app to presigned-URL upload and pass `audio_url` instead. The
worker already accepts it.

## Measurement

```bash
export RUNPOD_API_KEY=... RUNPOD_ENDPOINT_ID=...
python bench/measure.py health
python bench/measure.py cold  --audio track.mp3 --label "RTX 4090, weights baked in"
python bench/measure.py warm  --audio track.mp3 --n 5  --label "RTX 4090"
python bench/measure.py burst --audio track.mp3 --n 10 --label "RTX 4090"
python bench/measure.py report          # markdown tables from results.jsonl
```

`cold` warns if a worker is already up, because the single easiest way to publish a wrong
cold-start number is to measure a warm one. Results append to `bench/results.jsonl`; paste
`report` output into `../PHASE2.md`.

## Phase 2: weights on a network volume

The Dockerfile bakes weights in and sets `TORCH_HOME=/opt/torch`. To measure the
alternative, attach a network volume to the endpoint, copy the demucs cache onto it, and
set `TORCH_HOME=/runpod-volume/torch` as an endpoint environment variable — no rebuild.
Compare `cold` runs with and without. The trade is image pull time against volume read
time, and which wins is not obvious in advance, which is the point of measuring.

## Tuning segmentation

Everything in `segment.py` is a named constant with a keyword override:

| Knob | Default | What it does |
|---|---|---|
| `conf_threshold` | 0.5 | Periodicity gate. Raise it if silence produces phantom notes. |
| `median_window` | 5 | Frames of median filter in MIDI space. This is what kills octave jumps. |
| `hysteresis_st` | 0.6 | Extra semitones of slack before switching notes. Raise it if vibrato splits notes. |
| `switch_frames` | 2 | Frames a new pitch must hold before it counts. |
| `min_note_s` | 0.06 | Shorter runs are noise. |
| `max_gap_s` | 0.04 | Dropouts shorter than this don't split a note. |

If torchcrepe's low-frequency accuracy disappoints on real material — bass fundamentals run
~41Hz to ~400Hz and trackers get unreliable at the bottom — the fallback is `basic-pitch`,
at the cost of a second runtime and roughly 2GB of image.
