# Phase 2 — measurements

Measured on a live Runpod Serverless endpoint, RTX 4090 (`ADA_24`), scale to zero, FlashBoot
OFF, model weights baked into the image. Raw runs are in
`bass-transcribe-worker/bench/results.jsonl`; `python bench/measure.py report` renders them.

Nothing here is an estimate. Where a number was reasoned out rather than measured — GPU tier
selection, the scale-to-zero crossover — the section says so in its heading or opening line.

**Test track:** *Stereosity — New Life Will Grow — 09 Sonata in C#*, 204.4s, transcoded to
64kbps mono AAC (1.64MB). The same file for every warm and concurrency row. Cold-start rows
use a 3.2s synthetic tone from `tools/make_test_tone.py` instead, and are labelled as such —
separation time scales with audio length, so the two are not interchangeable.

Both endpoints used here have since been deleted; the numbers are not re-runnable without
recreating one.

---

## Cold start (measured)

These are from `tools/make_test_tone.py` — four open-string notes, 3.2s — used to verify
the endpoint end to end. **Separation time scales with audio length, so none of these
numbers transfer to a 4-minute track.** They are here for the cold-start finding only.

Endpoint `v22yhtuixccxc0`, RTX 4090 (`ADA_24`), FlashBoot OFF, weights baked in,
`workersMin` 0, `idleTimeout` 5s.

| Run | Condition | delayTime | exec | model load | separation | pitch | logged? |
|---|---|---|---|---|---|---|---|
| A | new endpoint, image never pulled anywhere | **217.7s** | 21.8s | 1.52s | 3.15s | 16.93s | by hand |
| B | new endpoint, image already exists in GHCR | **171.7s** | 21.1s | 1.56s | 2.09s | 17.02s | ✅ jsonl |
| C | same endpoint as B, drained to zero, refired | **144.3s** | 21.5s | 1.90s | 2.01s | 17.24s | ✅ jsonl |
| D | same endpoint, new tag, workers recently active | **15.2s** | 19.9s | 2.07s | 1.60s | 15.76s | by hand |

**Scale-from-zero cold start is ~145–220s. Consistently.** Three independent measurements
(A, B, C) all landed in that band, on a 4.04 GiB image.

**An earlier version of this document drew the wrong conclusion from run D.** It claimed
cold start was "not one number" — ~218s on a fresh host versus ~15s once layers were
cached — and treated 15s as the common case. Runs B and C, taken specifically to log the
number properly, refute that. D was not a scale-from-zero cold start at all: it was a
redeploy that landed on a host still holding the previous image, i.e. an in-place image
swap. Every genuine start-from-nothing measurement is in the 145–220s band.

The revised model: **what matters is whether the host assigned to your worker already holds
your image, and on a scale-to-zero endpoint it usually does not.** Global image popularity
does not help — run B paid 171.7s for an image Runpod hosts had already pulled several
times that day.

Consequences:

- A scale-to-zero endpoint serving a 4 GiB image makes its first user wait **2.5–3.5
  minutes**. That is a product constraint, not a tuning detail, and it dominates every
  other latency term by an order of magnitude.
- This *strengthens* the case for the network-volume experiment, which the earlier
  conclusion had dismissed. If the pull is 145–220s every time a worker starts cold, moving
  weights off the image is attacking the dominant cost rather than a rare one.
- Image size is the lever worth pulling. 4.04 GiB is mostly the CUDA base image, so a
  slimmer base plausibly beats every other optimisation available here.

**Where the cold-start time actually goes** (run C, 3.2s clip, 168.1s wall):

| | | share |
|---|---|---|
| Worker startup (pull + boot) | 144.3s | 86% |
| torchcrepe first-call init | ~17.2s | 10% |
| Demucs model load | 1.9s | 1% |
| **Actual inference** | **~2.0s** | **1%** |

Roughly 1% of a cold request is the work the user asked for.

**`pitch_s` is ~15s on a 3.2s clip and barely moves with audio length** — that is
torchcrepe loading its weights on first call, not inference. Every run above hit a cold
worker (`idleTimeout: 5` scales down almost instantly), so this cost appears every time.

That prediction held: on a warm worker pitch drops to 4.3–5.2s (see Warm execution), and the
concurrency burst measured both states side by side — 4.29–6.22s warm versus 18.78–24.89s
cold. So ~14–20s of a cold worker's pitch stage is torchcrepe initialisation, not inference.

---

## Cold start: weights baked in vs. network volume — NOT MEASURED

The measured cold-start numbers are in the section above. This section covers the one
cold-start experiment that was never run.

| Config | Wall | Pull + boot | Model load | Separation | Pitch |
|---|---|---|---|---|---|
| Weights baked into image | 168.1s | 144.3s | 1.9s | 2.0s | 17.2s |
| Weights on network volume | — | — | — | — | — |

**Status: open, and now the highest-value remaining measurement.**

This was originally deprioritised on the reasoning that layer caching already got the common
case to ~15s, so moving weights off the image would only help a rare first pull. **That
reasoning was based on a measurement that turned out not to be a cold start** (see above).
With every genuine scale-from-zero start landing at 145–220s, and 86% of a cold request
being worker startup, the pull *is* the common case.

To run it: attach a network volume, copy the demucs cache onto it, and set
`TORCH_HOME=/runpod-volume/torch` as an endpoint env var — no rebuild, the Dockerfile
already parameterises it. Then compare `measure.py cold` runs.

**Predict before measuring.** The image is 4.04 GiB, mostly the CUDA base layer, and the
demucs weights are only ~80MB of it. Moving 80MB off a 4 GiB image should barely help — the
pull is dominated by the base image, not the weights. If that holds, the real lever is a
slimmer base image, not a network volume, and this experiment's value is in ruling out the
obvious-sounding fix rather than finding one.

---

## Warm execution

Track: *Stereosity — Sonata in C#*, 204.4s, transcoded to 64kbps mono AAC (1.64MB).
RTX 4090 (`ADA_24`), FlashBoot OFF, weights baked in, warm worker. n=5.

| | Min | Median | Max |
|---|---|---|---|
| Separation | 6.31s | **6.68s** | 14.63s |
| Pitch tracking | 4.90s | **5.18s** | 7.18s |
| Worker total | 11.21s | **11.82s** | 20.09s |
| Round trip incl. payload | 12.44s | **13.27s** | 21.39s |
| Payload + queue overhead | 1.23s | **1.45s** | 2.12s |
| Notes returned | 359 | — | 370 |

**A 3:24 track transcribes in 13.3s warm — about 15x realtime end to end.** Separation and
pitch are closer in cost than expected: 6.7s vs 5.2s, so torchcrepe is not the free stage
the model-choice table implies. Model load is 0s on a warm worker, confirming the
`_load()` pattern does what it is supposed to.

**Is payload transfer a meaningful share of latency?** No — 1.45s of 13.27s, about 11%,
and most of that is queue rather than transfer. But that answer only holds *because* the
payload was shrunk to 1.64MB. See below.

### Payload transfer is not a latency problem, it is a reliability problem

The spec anticipated measuring payload transfer as a share of latency. The real finding is
different and worse: **base64 upload over this path fails most of the time.**

| Payload (base64) | Source | First-attempt outcome |
|---|---|---|
| 0.75 MB | 3.2s synthetic tone | never observed to fail |
| 2.19 MB | 204s track @ 64kbps mono | **4 of 5 runs needed a retry** |
| 3.28 MB | 204s track @ 96kbps mono | 3 of 4 failed outright |
| 11.11 MB | 204s track @ 320kbps stereo | 5 of 5 failed |

Failures are `Broken pipe` and, once, `SSL_ALERT_BAD_RECORD_MAC` — a corrupted TLS record,
which is transport-level, not the API rejecting anything. Failure probability rises with
size but there is no clean cutoff, so "the request is too big" is the wrong model.

The numbers above are achievable only with retries, which `measure.py` now does (timing
only the successful attempt, and recording `attempts` so the flakiness stays visible). **A
median of 13.27s alongside an 80% first-attempt failure rate would be a dishonest headline
on its own.**

**Conclusion: move to presigned-URL upload.** Not as the latency optimisation the spec
imagined, but because base64 in the request body is not a reliable transport at song size.
The worker already accepts `audio_url`; what is missing is somewhere to host the file.

### Two smaller findings

**Output is not deterministic.** Identical input returned between 359 and 370 notes across
five runs — a 3% spread, most likely from Demucs' overlapped-window splitting plus
non-deterministic GPU reductions. Worth knowing before treating any single eval number as
exact, and worth pinning down before tuning segmentation against small F1 differences.

**One separation ran 2.2x slow** (14.63s vs a 6.68s median) with no change in input. The
endpoint had throttled workers at the time, so this looks like host contention rather than
anything in the code. It is the reason to report min/median/max rather than a mean.

---

## Cost per job

From the billing API, all on RTX 4090 (`ADA_24`). GPU selection is covered further down —
it was derived rather than measured.

Final figures, after both endpoints' usage settled. Job counts are each endpoint's
`completed` counter, read immediately before deletion.

| Endpoint | Jobs | GPU | Fees | Disk | Total | Per job |
|---|---|---|---|---|---|---|
| `v22yhtuixccxc0` — verification, warm, burst | 23 | $0.321 | $0.156 | $0.001 | $0.479 | $0.021 |
| `qez02qfcx3n6cd` — cold-start re-measurement | 2 | $0.025 | $0.012 | — | $0.037 | $0.018 |
| **Total** | **25** | **$0.346** | **$0.168** | **$0.001** | **$0.515** | **~$0.021** |

Fees are **33% of the bill**, which is worth noting on its own: a third of the cost is not
GPU time.

**This number was misreported twice before landing here.** First as $0.030 total / ~$0.001
per job, read mid-session before usage had posted. Then as $0.479 / ~$0.012 per job, which
had the right total for one endpoint but used a guessed job count of ~40 instead of the
actual 25. The lesson is narrow and practical: **Runpod billing lags materially, so do not
quote cost until the endpoint has been deleted and the figures have settled.**

### A retracted claim: cold jobs are not obviously more expensive

An earlier version of this section asserted that a cold job costs ~13x a warm one (~$0.041
vs ~$0.003), derived by back-solving an effective hourly rate and multiplying by wall time.
**The per-endpoint split above contradicts it.**

Endpoint `qez02qfcx3n6cd` ran nothing but cold starts and cost **$0.018/job**. Endpoint
`v22yhtuixccxc0` ran 23 mostly-warm jobs and cost **more**, at $0.021/job. If cold starts
carried a 13x cost penalty, that ordering could not happen.

The most likely explanation is that the two endpoints were not configured alike:
`v22yhtuixccxc0` had `idleTimeout` raised from 5s to 120s for the warm and concurrency
tests, so its workers sat idle-but-billing between requests, and `workersMax` was raised to
10 for the burst. **Idle worker time, not cold start, plausibly dominates its cost.** A
secondary possibility is that Runpod does not bill the full image pull — the arithmetic on
endpoint 2 implies roughly 50s of billed time per job against ~181s of wall time — which
would be a genuinely customer-friendly detail if true.

Both explanations are inference from two coarse aggregates. **Separating warm from cold cost
properly needs per-job billing data, which the billing API does not expose at this
granularity.** Recorded as an open question rather than an answer.

The general lesson stands even though the specific number did not: cost per job is driven by
*billed worker seconds*, which includes time the worker is alive but not working. That makes
`idleTimeout` a cost lever, and it is the one setting here that was changed for measurement
convenience without considering its billing impact.

---

## Scale-to-zero vs. always warm

| | Value |
|---|---|
| Cost of one always-warm worker, 24h idle | ~$17.76/day (4090 at $0.74/hr) |
| Measured cost per job, scale-to-zero | ~$0.021 |
| Cold-start penalty per job | ~145–220s latency; cost penalty **not established** |
| **Jobs/day at which a warm worker pays for itself** | **~850** |

$17.76 ÷ $0.021 ≈ 846 jobs/day, or roughly one job every two minutes sustained. The hourly
rate is the *pod* price used as a proxy; serverless active-worker pricing differs, so treat
the crossover as an order-of-magnitude answer, not a threshold to plan against.

**Which would I ship? Scale-to-zero, and it is not close on cost.** At any plausible volume
for this workload the endpoint is idle almost all the time, and paying $17.76/day to avoid a
cold start that happens a few times a day is indefensible.

But the honest version has a caveat the cost table hides: **scale-to-zero makes the first
user of every idle period wait 2.5–3.5 minutes.** That is a bad enough experience that the
right answer is probably neither pure option — it is scale-to-zero plus an async API and a
progress UI, so the wait is visible and expected rather than looking like a hang. The
earlier `/runsync` bug is a symptom of the same thing: a synchronous request/response shape
does not fit a workload whose cold path is three minutes long.

The genuinely interesting lever is not warm-vs-cold, it is **making cold cheaper** — a
smaller image attacks the 86% of a cold request that is worker startup, and would improve
latency and cost per job at the same time.

---

## Concurrency

`workersMax` raised 3 → 10. Starting state: 1 ready worker, 1 initializing, 4 throttled.

| Concurrent requests | p50 | p95 | Batch wall | Failures |
|---|---|---|---|---|
| 1 (warm) | 13.27s | ~21s | — | 0 |
| 10 | **66.86s** | **89.89s** | 105.33s | 1 of 10 |

**Scale-up does not keep pace.** Execution across the 9 successful jobs totalled 158s and
finished in a 105s window — effective parallelism of about **1.5x**, against a configured
max of 10 workers. Queue delay was 39–73s per request versus 150ms on an idle endpoint, so
nearly all the degradation is waiting, not slower execution.

Runpod did react: 4 requests landed on brand-new workers (`cold: true`, uptime 1–2s), so
the QUEUE_DELAY autoscaler fired. But new workers need ~15s to come up even with layers
cached, and 4 of the pool's workers were throttled throughout. Scale-up was real and too
late to matter.

**For a bursty consumer workload this is the shape that matters:** p50 degrades 5x and p95
nearly 7x the moment ten users arrive together. That is an argument for a queue with
honest progress reporting in the UI, not for a bigger GPU.

### The burst isolated torchcrepe's first-call cost

Cold and warm workers served the same job in the same batch, which separates a cost the
other runs conflated:

| Worker state | Pitch stage |
|---|---|
| Warm | 4.29 – 4.74s |
| Cold | 18.78 – 24.89s |

**14–20s of the pitch stage on a cold worker is not inference.** It is torchcrepe's first
call — weight load plus, most likely, CUDA context initialisation, which the demucs load in
`_load()` does not pay because it happens before any CUDA op.

**Actionable: warm both models in `_load()`.** Run a throwaway inference through demucs and
torchcrepe when the worker boots, so the first real request does not absorb CUDA init. This
does not reduce total cold start — the cost moves rather than disappears — but it moves it
out of the request path, makes `model_load_s` honest, and means the first user of a cold
worker is not the one who pays. Worth measuring rather than assuming: the gain depends on
whether Runpod bills worker boot time the same as request time.

---

## GPU selection — not measured, derived

**Deliberately skipped**, because the numbers already collected answer the question and the
measurement would cost more than it teaches. Stating the reasoning rather than leaving an
empty table:

| Term | Value | GPU-dependent? |
|---|---|---|
| Cold start, layers cached | ~15s | no — image pull and container boot |
| Cold start, fresh host | ~218s | no |
| Queue delay under load | 39–73s | no — scheduling |
| Separation | 6.68s | **yes** |
| Pitch (warm) | 5.18s | **yes** |
| Payload + queue, idle | 1.45s | no |

Only ~11.9s of a 13.3s warm round trip is GPU-bound, and that is the *best* case. Under
concurrency the GPU-bound share falls to roughly 18% of a 66.9s p50. A GPU that made
inference literally instant would take the warm round trip from 13.3s to ~1.4s and the
loaded p50 from 66.9s to ~55s — while costing 2.2x (L40S) to 4.4x (H100) per hour.

**At what job volume does a faster, pricier GPU become cheaper per job? For this workload,
never.** Cost per job scales with billed seconds, and billed seconds are dominated by cold
start and queueing, neither of which a faster GPU improves. The A4000 tier at $0.25/hr is
the rational choice, and the interesting optimisations are all elsewhere: cold start, the
upload path, and torchcrepe's first-call cost.

This is reasoning from measurements, not a measurement. It would be falsified if separation
scaled worse than linearly with track length on cheaper GPUs, or if the cheap tier's LOW
availability pushed queue delay up enough to swamp the hourly saving — both worth a check
before treating it as settled.

---

---

## Conclusions

Three or four sentences. What surprised you, what you would change, and what you would
ship.
