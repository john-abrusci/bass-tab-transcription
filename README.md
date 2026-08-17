# Bass Tab Transcription

Upload an audio file, get a bass tab. GPU inference runs on Runpod Serverless; the app
layer is Next.js.

## Results

Deployed and measured on a live Runpod Serverless endpoint (RTX 4090, `ADA_24`, scale to
zero). Full detail in [`PHASE2.md`](PHASE2.md).

| | |
|---|---|
| Image | 4.04 GiB compressed, 13 layers, built on CI for native amd64 |
| Cold start | **145–220s** scale-from-zero, consistent across 3 measurements |
| Warm round trip | **13.3s** median for a 3:24 track — about 15x realtime |
| Split | separation 6.7s, pitch 5.2s, payload + queue 1.4s, model load 0s |
| Under load | 10 concurrent requests take p50 to **66.9s**, p95 to 89.9s |
| Cost | **$0.515 for 25 jobs — ~$0.021/job**, of which 33% is fees rather than GPU |
| End to end | 365 notes from a real track, rendered as tab, zero DP pitch mismatches |

**Four findings worth more than the numbers:**

**Cold start dominates everything else, and it does not improve with familiarity.** Three
independent scale-from-zero measurements landed at 217.7s, 171.7s and 144.3s on a 4.04 GiB
image — including one on an image Runpod's hosts had already pulled repeatedly that day.
Roughly 86% of a cold request is worker startup and about 1% is the inference the user
asked for. An earlier version of this README claimed a ~15s cached case was typical; that
measurement turned out to be a redeploy landing on a host still holding the previous image,
not a cold start. See `PHASE2.md` for the correction.

**Base64 upload is a reliability problem, not a latency one.** The spec expected to measure
transfer as a share of latency; it is 11%. The real result is that upload fails most first
attempts — 5/5 at 11MB, 3/4 at 3.3MB, 4/5 needing a retry at 2.2MB, including a corrupted
TLS record. Presigned URLs are the answer, for a different reason than anticipated.

**Scale-up does not keep pace.** Ten simultaneous requests achieved ~1.5x effective
parallelism against `workersMax: 10`. Nearly all degradation is queue wait, not slower
execution.

**GPU choice barely matters here.** Only ~11.9s of a 13.3s warm round trip is GPU-bound,
falling to ~18% under load, against a cold start of 15–218s. The spec asks at what volume a
pricier GPU becomes cheaper per job; for this workload, never. Derived from the measurements
rather than measured directly — the reasoning is in `PHASE2.md`.

### Notes from the user's seat

Deploying this meant using Runpod Serverless as a customer for the first time, so I kept a
log of everywhere the product surprised, blocked or delighted me:
**[`FRICTION-LOG.md`](FRICTION-LOG.md)** — seven observations ranked by what I would fix
first, plus what the product does well and what I would deliberately not prioritise.

It is a snapshot, not a verdict: everything in it was measured on 12 August 2026 from a
single client against one GPU pool over a few hours, and one of the seven items is ranked
*below* its apparent severity because I could not isolate whether the cause was the platform
or my own network. Runpod ships continuously and some of it may already be out of date.

## Architecture

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

## Status

| Phase | | Notes |
|---|---|---|
| 0–1 Worker | deployed, verified on hardware | `tools/verify_endpoint.py` asserts known pitches round-trip |
| 2 Measurement | cold, warm, payload, concurrency done | GPU tiers derived not measured; scale-to-zero economics open |
| 3 Fretboard DP | works on real output | 362/365 notes placed, 0 pitch mismatches; weights untuned against ground truth |
| 4 Tab + eval | renders real tabs; F1 **1.000** on the sampled fixture | 3 fixtures, exact ground truth; no *recorded* bass yet |

**Verified:** `npm test` (25 assertions), `python test_segment.py` (16 assertions across 12
cases), typecheck, production build, the Docker image building on CI, and a full path from
audio file through the app to a rendered tab using live GPU inference.

**Accuracy, measured:**

| Fixture | Bass | Backing | Note F1 | Pitch acc | Octave err |
|---|---|---|---|---|---|
| Synthetic over synthesised | harmonic stack | synthesised | 0.971 | 100% | 0% |
| Synthetic over real | harmonic stack | real, bass removed | 0.995 | 100% | 0% |
| **Sampled over real** | **GM electric bass** | real, bass removed | **1.000** | 100% | 0% |

Ground truth for all three comes from `tools/make_eval_track.py`, which synthesises a 32-bar
bassline so every onset and pitch is exact by construction. This sidesteps the reason
scraped human tabs cannot be used directly: they carry no timestamps, so onset-based F1
against them would need audio-to-tab alignment first.

The realistic fixture needs backing that has no bass of its own, or Demucs would separate
two basslines and the ground truth would be meaningless. `tools/strip_bass.py` produces it
from any recording by separating the track and keeping drums + other + vocals.

**Real backing scored higher than synthesised backing, which was not the expectation.**
Demucs is trained on real music, so real drums, guitars and vocals are its training
distribution and it separates them cleanly. The synthesised chords were both
out-of-distribution and spectrally careless — pitched at 330–500Hz, colliding with the
`BASS_FMAX = 500` search range. The evidence is in the MIDI ranges above: against synthesised
backing the pipeline invented notes down at MIDI 29, well below the true 40–59; against real
backing it produced exactly 40–59.

**Sampled bass closed the timbre gap, and timbre turned out not to matter.**
`tools/make_sampled_bass.py` renders the identical note pattern through Apple's built-in GM
soundbank (program 33, electric bass finger) via FluidSynth — real sampled audio, no
third-party download. Its spectrum is markedly richer than the synthesised stack: 20.5% of
energy in 300–800Hz versus 7.7%, and 8.4% above that versus 1.7%. It scored the same 0.995,
note for note, on the same two errors.

**Those two errors then turned out to be a real bug, not a limit.** Both fixtures missed the
same thing: a pitch repeated across a bar boundary with a 30ms unvoiced gap, which
`segment.py` merged into one note because `max_gap_s` was 0.04. Lowering it to 0.02 splits
the re-attack while still bridging 20ms dropouts, taking the sampled fixture to **F1 1.000**
with no regression on the others. Locked in by `test_same_pitch_reattack_splits`.

That is the eval harness doing the job it exists for: a specific defect, localised to a
named constant, fixed with evidence, and a regression test so it stays fixed.

**Still unmeasured: a genuinely recorded bass.** Apple's DLS bank is 2MB, so its samples are
short and loop-based, and every note here is MIDI-quantised at constant velocity. Real
playing dynamics, timing humanisation, fret noise and amp character remain untested — a DI
recording of a known part is the remaining step, and it reintroduces hand annotation.

**Still unmeasured: accuracy on real recorded music, and position accuracy.** Both need
human-tabbed songs. A synthesised note has no "correct" fingering, so the fixture omits
string/fret and the harness reports `n/a` rather than 0% — "not measured" and "measured and
wrong" must not look the same in a results table.

Known rough edges visible in real output: notes detected up to C4 (above where a bassline
lives, likely bleed surviving separation), a few below the low E, and 38% of track duration
with no detected notes — some genuine silence, some probably missed. `segment.py` gates to
midi 24–72, which is too generous. Tightening it is a one-line change deliberately **not**
made yet, because without eval there is no way to tell whether it removes errors or real
notes.

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

### Transcode before uploading

Base64 upload fails most first attempts at song size (see Results). Until the app does this
in the browser, shrink the file first — the pipeline downmixes to mono and discards
everything above 500Hz, so bitrate costs you nothing here:

```bash
# macOS, no ffmpeg needed. 8.3MB @ 320kbps stereo -> 1.6MB @ 64kbps mono.
afconvert -f m4af -d aac -b 64000 -c 1 track.mp3 track_64k_mono.m4a
```

Every measurement in `PHASE2.md` used a file prepared exactly this way.

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
