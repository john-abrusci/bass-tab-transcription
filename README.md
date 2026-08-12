# Bass Tab Transcription

Upload an audio file, get a bass tab. GPU inference runs on Runpod Serverless; the app
layer is Next.js.

```
Next.js app
  │  POST audio → /runsync
  ▼
Runpod Serverless endpoint (one endpoint, both stages)
  ├─ Stage 1: htdemucs        → isolated bass stem
  └─ Stage 2: torchcrepe      → f0 track → note events
  │  ← JSON note list + timings
  ▼
Next.js app
  ├─ Stage 3: fretboard assignment (DP over hand position)
  └─ Stage 4: tab rendering + evaluation harness
```

Built to [`bass-tab-transcription-spec.md`](bass-tab-transcription-spec.md).

## Layout

```
bass-transcribe-worker/     Phase 0-1: the Runpod worker
├── handler.py              request → notes, with the per-worker model cache
├── transcribe.py           separation + pitch tracking
├── segment.py              f0 frames → note events
├── test_segment.py         segmentation tests (no GPU, no torch)
├── bench/measure.py        Phase 2: cold start / warm / concurrency measurement
└── tools/encode.py         build a test_input.json from an audio file

web/                        Phase 3-4: the app layer
├── app/                    upload UI + /api/transcribe route
├── lib/fretboard.ts        DP over (string, fret)
├── lib/tab.ts              ASCII tab renderer
├── scripts/eval.ts         note F1 / pitch accuracy / position accuracy
└── eval/                   ground truth and predictions

PHASE2.md                   the measurement checklist, ready to fill in
```

## Quickstart

The app runs without a GPU or a Runpod account. With `RUNPOD_*` unset the API route
serves a synthetic bassline flagged `source: "mock"`, so the DP, the renderer and the
eval harness are all fully exercisable offline.

```bash
cd web
npm install
npm run dev            # http://localhost:3000
npm test               # fretboard DP + renderer
npm run eval -- --sweep

cd ../bass-transcribe-worker
python test_segment.py # segmentation, pure numpy/scipy
```

To point at a real endpoint, put `RUNPOD_API_KEY` and `RUNPOD_ENDPOINT_ID` in
`web/.env.local` (see `.env.example`) and follow
[`bass-transcribe-worker/README.md`](bass-transcribe-worker/README.md) to build and deploy.

## Decisions worth defending

**One endpoint, not two.** Separation and pitch tracking share a process, so the stem
never leaves memory. Two endpoints would mean two cold starts and a stem round-trip over
the network. The cost is a larger image — the right trade when end-to-end latency is the
metric.

**torchcrepe over basic-pitch.** One runtime instead of two, ~2GB smaller image, faster
cold start. The price is writing note segmentation by hand, which turns out to be an
advantage: `segment.py` is where output quality lives and it is tunable and unit-tested.

**Model weights baked into the image.** Cold starts don't download anything. `TORCH_HOME`
is set in the Dockerfile so the Phase 2 network-volume comparison is a one-line change.

**Lazy module-level model load.** `_load()` in `handler.py` puts the weights in GPU memory
on a worker's first request and keeps them there. Loading inside the request path would
make every call pay model-load cost.

**`timings` in every response.** Separation, pitch, model load and a `cold` flag come back
with the notes, so Phase 2 numbers fall out of normal operation instead of a separate
instrumentation pass.

**Fretboard assignment is a DP, not a lookup.** Each pitch has 2–4 playable positions and
the right one depends entirely on where the hand already is — a shortest-path problem over
`(string, fret)`.

## Status

| Phase | | Notes |
|---|---|---|
| 0–1 Worker | code complete, unbuilt | Docker image has never been built — see below |
| 2 Measurement | tooling ready, no data | `bench/measure.py` + `PHASE2.md` await a live endpoint |
| 3 Fretboard DP | done, tested | live weight controls in the UI |
| 4 Tab + eval | done, tested | harness runs on a synthetic fixture |

**What has actually been verified here:** `npm test` (25 assertions on the DP and
renderer), `python test_segment.py` (15 assertions across 11 segmentation cases),
`npm run typecheck`, `npm run build`, and an end-to-end POST through `/api/transcribe`
returning a rendered tab.

**What has not:** the Docker image was never built and no GPU inference has run — there is
no Docker daemon and no Runpod endpoint in this environment. `transcribe.py`'s calls into
`demucs` and `torchcrepe` are written against their documented APIs but have not been
executed. Expect the first real build to need at least one round of dependency fixing.
Every number in `PHASE2.md` is blank on purpose; none of them are estimates.

## A note on the eval fixture

`web/eval/` ships a synthetic ground-truth/prediction pair so `npm run eval` runs out of
the box. Its "human" fingering comes from a naive rule (lowest string with a fret under 6),
which is why `--sweep` reports that ever-higher `fretHeight` scores better: it is
recovering the rule, not measuring musicality. `fretHeight` is left at a musically sensible
0.35 rather than the 1.0 the sweep prefers. Replace the fixture with real human tabs before
reading anything into these numbers.

## Legal

Personal use, on music you own. Private transcription is fine; running this as a service
for arbitrary uploads is a different question. Scraped tabs are evaluation ground truth
only, never product content.
