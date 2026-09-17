# Phase 3 — FlashBoot A/B, and the `/runsync` boundary

Measured 2026-09-17 on a live Runpod Serverless endpoint created for this experiment.
Raw rows are in `bass-transcribe-worker/bench/results.jsonl` (`kind: "flashboot_ab"`).

**Every number below was collected by the harness.** Nothing was captured by hand. Where a
statement is inference rather than measurement it says so inline.

## The headline

**FlashBoot made no measurable difference to cold start on this workload.**

From a warm baseline, 5 trials on and 5 off: median startup **52.4s off vs 52.3s on**, a
**0.1% difference** against a within-arm spread of ~2-3s. Mann-Whitney U = 8 (n=5,5;
significance would need U <= 2). The difference is **two percent of the noise**.

Getting to that number took two rounds, and the first round is the more useful story:

**Round 1 measured a 46% "improvement" that was not real.** Cold start fell from 186s to
51s across nine trials **regardless of the setting** — the variable that moved was trial
order, not FlashBoot. On this workload the dominant term is whether the assigned host
already holds the 4 GiB image, and it swamps the thing being tested by roughly 3.6x.

**Round 2 removed that variable** by warming the pool to its ~51s floor first, confirming
the plateau, and only then running the A/B. With the large effect held constant, the small
one is measurable — and it is approximately zero.

---

## Setup

| | |
|---|---|
| Endpoint | `2gg7cndhtlltrn`, created 2026-09-17 |
| GPU | RTX 4090 (`ADA_24`), $1.10/hr serverless |
| Data center | **US-IL-1, pinned** (deviation — see below) |
| Image | `ghcr.io/john-abrusci/bass-transcribe@sha256:7d622b69…` — **pinned by digest** |
| Image size | 4.04 GiB compressed, 13 layers, largest layer 3.53 GiB |
| Test clip | 3.2s synthetic tone, sha256 `f7209b6d…`, recorded on every row |
| Workers | min 0, max 1, idleTimeout 1s (the API floor) |
| Scaler | `QUEUE_DELAY`, value 4 |

**Pinned by digest, not tag.** CI builds on any push touching `bass-transcribe-worker/**`,
and `measure.py` lives there — committing this phase's harness changes would have rebuilt
and republished `:latest` mid-experiment, changing the layer cache between trials. A digest
cannot be moved. Same bytes for all nine trials, recorded per row.

**`max: 1` is deliberate**, so there is never ambiguity about which worker served a job.
Phase 2 used max 3.

**US-IL-1 is pinned, and that is a deviation from Phase 2**, which let the scheduler place
workers. The first worker this endpoint created landed in US-NC-1 and sat `THROTTLED`
indefinitely — 4090 stock there is LOW — holding the single worker slot so no job could
ever run. Pinning to the one data center with HIGH 4090 availability was necessary to
measure anything. **Consequence: Phase 2's numbers and these are not like-for-like.**

## Design: interleaved, not blocked

Trials alternated `OFF, ON, OFF, …` rather than three of each in a block.

This was a deliberate correction to a mistake Phase 2 made and had to retract. Phase 2's
run D looked like a 15s cold start and was actually a redeploy onto a host that still held
the image. Cold start here depends heavily on **whether the assigned host already has the
4 GiB image**, and in a blocked design that variable moves with trial order — which is
perfectly correlated with the condition.

**That decision is what produced the finding.** See "Why blocking would have lied" below.

FlashBoot was toggled **via the API** (`PATCH /v1/endpoints/{id}`, `{"flashboot": bool}`),
not the console. Every trial read the setting back from the control plane before firing
and voided itself on disagreement.

---

## Results

### Cold start by trial order — the actual finding

| Seq | FlashBoot | delayTime | wall | startup share | worker uptime at request |
|---|---|---|---|---|---|
| 1 | OFF | **186.2s** | 204.3s | 91% | 1.58s |
| 4 | **ON** | **100.7s** | 119.9s | 84% | 1.61s |
| 7 | OFF | **51.7s** | 72.1s | 72% | 1.59s |
| 9 | OFF | **51.0s** | 72.0s | 71% | 1.54s |

Cold start falls monotonically with trial order and **crosses the condition boundary
without noticing it**. A FlashBoot-OFF trial run late (51.7s) beat the FlashBoot-ON trial
(100.7s) by half. Two independent late OFF trials agree to within 0.7s (51.7 / 51.0),
which suggests a floor once the host pool holds the image.

`worker_uptime_at_request_s` is 1.5–1.6s on every row: each job reached a worker that was
under two seconds old. These are genuine cold starts, not warm workers mislabelled.

### FlashBoot ON vs OFF — as requested, and why it is not a result

| FlashBoot | n | median delayTime | delayTime spread | median wall | wall spread |
|---|---|---|---|---|---|
| OFF | 3 | 51.7s | 51.0 – 186.2s | 72.1s | 72.0 – 204.3s |
| ON | 1 | 100.7s | — (single trial) | 119.9s | — |

Stated plainly, as asked: **median delayTime OFF→ON is 51.7s → 100.7s, +95%. Median wall
is 72.1s → 119.9s, +66%.**

**Do not use those numbers.** They say FlashBoot nearly doubles cold start, which is
almost certainly false. The single ON trial ran 4th, between an OFF trial at 186.2s and OFF
trials at 51.7s — it sits exactly where the order trend predicts, and its value is better
explained by when it ran than by the setting. The OFF arm's spread (51.0–186.2s) is 3.6x,
far wider than any gap between the arms.

**With n=1 in the ON arm, FlashBoot's effect on this workload is unmeasured.** Not "small",
not "zero" — unmeasured.

### Why blocking would have lied

Had this run as originally planned — three OFF then three ON — the OFF block would have
drawn the early, cold-pool trials and the ON block the late, warm-pool ones. The table
would have read something like 186s → 51s and produced a clean, quotable **"FlashBoot cuts
cold start ~70%"** that was entirely an artifact of trial order.

The interleaved design plus a late OFF trial is the only reason that is visible. This is
the same failure mode as Phase 2's run D, caught before publication rather than after.

### Does the 86% image-pull share still hold?

**No — it is a function of how cold the pool is, not a constant.**

| Seq | FlashBoot | delayTime | wall | startup share |
|---|---|---|---|---|
| 1 | OFF | 186.2s | 204.3s | **91%** |
| 4 | ON | 100.7s | 119.9s | **84%** |
| 7 | OFF | 51.7s | 72.1s | **72%** |
| 9 | OFF | 51.0s | 72.0s | **71%** |

Phase 2's 86% was measured on a cold pool and is reproduced here (91% on the first trial).
But as the pool warms, startup falls while in-worker execution stays flat, so startup's
share drops to ~71%. **Startup still dominates in every case** — the cheapest cold request
measured was still 71% waiting for a worker — but "86%" is a point on a curve, not a
property of the workload. Whether FlashBoot shifts that breakdown is **not answerable
here**, because the single ON trial cannot be separated from its position in the order.

The in-worker side barely moved:

| Seq | FlashBoot | model load | separation | pitch | worker total | notes |
|---|---|---|---|---|---|---|
| 1 | OFF | 0.58s | 1.20s | 14.14s | 15.93s | 4 |
| 4 | ON | 0.54s | 1.19s | 14.80s | 16.52s | 4 |
| 7 | OFF | 0.63s | 1.56s | 16.58s | 18.78s | 4 |
| 9 | OFF | 0.79s | 1.80s | 17.42s | 20.01s | 4 |

All four returned the correct 4 notes, so these are complete end-to-end runs. `pitch` is
14–17s on a 3.2s clip on every row — torchcrepe's first-call initialisation, reproducing
Phase 2's finding, and unaffected by FlashBoot in the one trial that had it on.

---

## The `/runsync` timeout boundary

**Measured, and the tightest number in this document.**

| Seq | returned after | HTTP | body `status` | body keys |
|---|---|---|---|---|
| 1 | **90.30s** | **200** | `IN_QUEUE` | `["id", "status"]` |
| 4 | **90.31s** | **200** | `IN_QUEUE` | `["id", "status"]` |

Two observations 10 basis points apart: **the window is a fixed ~90 second timeout**, not a
variable or load-dependent one.

What the caller actually receives, verbatim:

```json
{"id":"sync-c7512f96-a4b3-406e-b3b4-a622ab31c075-u2","status":"IN_QUEUE"}
```

**The critical detail: this is not an error.** HTTP 200, no error field, a body containing a
job `id`. Client code that branches on `response.ok`, or on a 2xx status, sees success —
and a body with an `id` in it looks like a completed submission. The job is still queued and
the result is somewhere else entirely, reachable only by polling `/status/{id}`.

Every cold start measured here (51–186s) is shorter than 90s only at the warm end of the
range, so on a genuinely cold endpoint `/runsync` crosses this line routinely. Phase 2
inferred the window existed from its behaviour; this pins it to a number and a body shape.

**What to say about it:** `/runsync` is not synchronous in any useful sense for a workload
whose cold path exceeds 90 seconds. It is a 90-second optimistic wait with a silent
fallback to async, and the fallback is indistinguishable from success unless you read
`status`.

---

## What it took to get an endpoint to zero — a product finding

A cold-start measurement needs zero workers at submit time. That was the hardest part of
this phase.

**The endpoint reports `workersStandby: 1`, and that field is read-only.** It is rejected by
`PATCH /v1/endpoints/{id}` (`"key provided in request body which is not in input schema"`)
and is not a member of `EndpointInput` on the GraphQL API either. It was never set
deliberately — the endpoint was created with `workers.min = 0`, and both the REST and MCP
views confirm `workersMin: 0`.

Its effect is not cosmetic. With the slot available and **zero jobs ever submitted**, the
scheduler brought a worker to `ready` and held it there. An endpoint in that state never
reaches zero, and no cold start can be observed.

The workaround: set `workersMax = 0`, which terminates workers regardless of standby; wait
for a settled zero; restore the slot; submit immediately. The gap between restoring the slot
and submitting is recorded per trial (`slot_restore_to_submit_s`, 0.45s on the first trial)
rather than assumed away.

**On this endpoint, "scale to zero" was not reachable through Runpod's documented API
surface.** It had to be forced by taking the worker slot away. That belongs in the friction
log independently of anything measured here.

## Trials thrown out

Nine attempted, four valid. Every discard is recorded in `results.jsonl` with its reason.

| Seq | Condition | Outcome |
|---|---|---|
| 1 | OFF | ok |
| 2 | ON | VOID — `Broken pipe` |
| 3 | OFF | VOID — `Broken pipe` |
| 4 | ON | ok |
| 5 | OFF | VOID — `SSLV3_ALERT_BAD_RECORD_MAC` |
| 6 | ON | VOID — `Broken pipe` |
| 7 | OFF | ok |
| 8 | ON | VOID — submit returned HTTP 409 |
| 9 | OFF | ok |

Five losses were transport faults on the submit path — the same `Broken pipe` and
`SSL_ALERT_BAD_RECORD_MAC` failures Phase 2 documented for base64 uploads, here hitting a
0.75 MB payload. They are **not** endpoint properties and not contamination; the harness
voided them rather than reporting numbers.

Two mitigations were added mid-phase and both earned themselves back: per-trial isolation
(one transport fault costs one trial, not the run) and whole-trial retry that **re-drains to
zero first**, so a retry is still measuring a cold start. Seq 8's HTTP 409 is that guard
working — the first attempt's job had actually landed, so the resubmit was refused rather
than silently double-submitting and corrupting the measurement.

Switching the submit path from `/runsync` (holds a connection ~90s) to `/run` (returns in
0.29s) removed most of the failure surface, and was only affordable because the boundary
question had already been answered twice.

**The failures were not condition-neutral**, and that is the phase's real cost: four of five
ON trials were lost, versus two of four OFF. That is why the ON arm has n=1.

---

---

## Round 2 — 5x5 from a warm baseline

Round 1 established that cold start falls to a floor of ~51s once the host pool holds the
image, and that two independent trials at that floor agree to within 0.7s. That floor is a
usable baseline: with the order effect saturated, a real FlashBoot effect should show.

**Method changes from round 1**, all aimed at the things that cost round 1 its power:

| | Round 1 | Round 2 |
|---|---|---|
| Starting state | cold pool | **warmed to the ~51s plateau, confirmed at 52.2s** |
| Order | strict alternation | **pre-declared counterbalanced** `OFF,ON,ON,OFF,ON,OFF,OFF,ON,OFF,ON` |
| Submit path | `/runsync` (holds ~90s) | **`/run`** (returns in ~0.3s) |
| Payload | 735 KB base64 | **133 KB** (mono 16 kHz, same 3.2s tone) |
| Gates | flashboot, workers zero | **+ queue empty, + worker provably fresh** |
| Valid trials | 4 of 9 | **10 of 10** |

The counterbalanced order is not strict alternation, so a periodic external effect with a
two-trial period cannot align with the conditions. It is pre-declared rather than randomised
at runtime so the sequence is reproducible.

### Every trial

| Seq | FlashBoot | outcome | delayTime | wall | exec | worker uptime |
|---|---|---|---|---|---|---|
| 1 | OFF | ok | 52.4s | 69.8s | 15.9s | 1.49s |
| 2 | ON | ok | 52.3s | 67.7s | 14.8s | 1.54s |
| 3 | ON | ok | 49.9s | 65.8s | 14.8s | 1.48s |
| 4 | OFF | ok | 50.7s | 65.9s | 14.8s | 1.52s |
| 5 | ON | ok | 53.4s | 72.0s | 16.7s | 1.48s |
| 6 | OFF | ok | 56.5s | 74.2s | 16.5s | 1.42s |
| 7 | OFF | ok | 55.8s | 74.2s | 16.8s | 1.53s |
| 8 | ON | ok | 54.1s | 74.3s | 19.7s | 1.46s |
| 9 | OFF | ok | 50.6s | 69.9s | 17.4s | 1.47s |
| 10 | ON | ok | 50.1s | 72.0s | 19.8s | 1.39s |

### FlashBoot ON vs OFF

Valid: **5 OFF, 5 ON** of 10 attempted.

| FlashBoot | n | median delayTime | spread | median wall | spread |
|---|---|---|---|---|---|
| OFF | 5 | 52.4s | 50.6–56.5s | 69.9s | 65.9–74.2s |
| ON | 5 | 52.3s | 49.9–54.1s | 72.0s | 65.8–74.3s |

**delayTime: 52.4s → 52.3s, -0.1%.**
**Wall: 69.9s → 72.0s, +3.0%.**

- OFF range 50.6–56.5s, ON range 49.9–54.1s — **ranges OVERLAP**.
- within-arm stdev: OFF 2.8s, ON 1.9s; between-arm gap 0.1s.
- gap / pooled stdev = **0.02**. Below ~1 the arms are not distinguishable at this n; a difference smaller than the noise is not an effect.
- Mann-Whitney U = **8** (n=5,5). Two-tailed p<0.05 needs U ≤ 2, so this is **NOT significant** at alpha=0.05.

### In-worker breakdown

| Seq | FlashBoot | model load | separation | pitch | worker total | notes |
|---|---|---|---|---|---|---|
| 1 | OFF | 0.58s | 1.15s | 13.9s | 15.63s | 4 |
| 2 | ON | 0.51s | 1.06s | 12.97s | 14.54s | 4 |
| 3 | ON | 0.49s | 1.04s | 13.09s | 14.62s | 4 |
| 4 | OFF | 0.51s | 1.06s | 13.03s | 14.61s | 4 |
| 5 | ON | 0.68s | 1.67s | 14.09s | 16.44s | 4 |
| 6 | OFF | 0.66s | 1.17s | 14.44s | 16.26s | 4 |
| 7 | OFF | 0.57s | 1.39s | 14.53s | 16.5s | 4 |
| 8 | ON | 0.67s | 1.38s | 17.42s | 19.47s | 4 |
| 9 | OFF | 0.58s | 1.24s | 15.34s | 17.18s | 4 |
| 10 | ON | 0.62s | 1.31s | 17.57s | 19.51s | 4 |

### Startup share of a cold request

| FlashBoot | median delayTime | median exec | median wall | startup share |
|---|---|---|---|---|
| OFF | 52.4s | 16.5s | 69.9s | **75%** |
| ON | 52.3s | 16.7s | 72.0s | **73%** |

### Reading this

**The arms are indistinguishable.** The ON and OFF ranges overlap almost completely
(50.6-56.5 vs 49.9-54.1), the gap between medians is 0.1s against a pooled standard
deviation of 2.4s, and the rank test does not come close to significance.

**Every trial is a verified cold start.** `cold: true` on all ten, and
`worker_uptime_at_request_s` between 1.39s and 1.54s — each job reached a worker under two
seconds old. That gate matters: round 2's first attempt produced a 10.9s "cold start" that
was really a job landing on a worker which had been booting during a retry backoff, and a
153s one that was a job queued behind a leftover job, cold-starting a worker that then
served ours warm. Both were caught and voided rather than reported.

**Startup share is now ~73-75%**, down from 91% on a cold pool. Startup still dominates —
even the cheapest cold request is three-quarters waiting for a worker — but the 86% from
Phase 2 is a point on a curve, not a constant.

**A drift worth noting:** in-worker execution rises across the run (14.5s at trial 2 to
19.5s at trial 10) while startup does not trend. That is orthogonal to the A/B — it is
spread across both arms by the counterbalancing — but it is unexplained, and it is the
reason `wall` shows +3.0% while `delayTime` shows -0.1%. **Do not read that +3.0% as a
FlashBoot cost.**

### What this does and does not settle

**Settled, for this configuration:** with the host pool warm, turning FlashBoot on does not
measurably reduce cold start for a 4 GiB image on RTX 4090 in US-IL-1. If someone proposes
FlashBoot as the fix for a three-minute cold start here, this is the evidence against.

**Not settled:** whether FlashBoot helps on a *cold* pool. That is the case where the 186s
starts live, and it is the case this experiment could not isolate — the order effect there
is larger than anything FlashBoot could plausibly contribute, which is precisely why round 1
failed. **Inference, not measurement:** FlashBoot restores a snapshot of a worker that has
already run on a machine, so on a cold pool there is likely no snapshot to restore, and the
mechanism predicts little benefit exactly where the pain is. That prediction is untested.

## Still unmeasured — and what not to claim

**Do not claim FlashBoot helps cold start here.** Measured, 5v5 from a warm baseline:
-0.1% on startup, not significant. That is a null result for this configuration.

**Do not state it as "FlashBoot does nothing" either.** A null result at n=5 per arm bounds
the effect; it does not prove zero. What this supports: *any* effect is small relative to a
~2-3s within-arm spread, so it cannot explain a three-minute cold start. An effect of a few
seconds would not have been detected.

**Do not carry round 1's numbers forward.** The 186s -> 100.7s -> 51.7s sequence shows the
order effect, not a FlashBoot effect. The "+95%" and "46%" figures that appear in the round 1
section are both artifacts and are labelled as such.

**Do not extend this to a cold pool.** The measurement holds the host pool warm. On a cold
pool — where the 186s starts live — FlashBoot's effect is untested, and that is the case
anyone actually cares about. *(The mechanism predicts little help there, since there is no
snapshot to restore on a host that has never run the image. Inference, not measured.)*

**Do not claim anything about a first-ever cold start.** By construction FlashBoot cannot
help a worker starting where this image has never run — there is no snapshot. Every trial
here followed at least one prior run in the same data center. *(Inference, not measured.)*

**Host identity is not observable.** Runpod does not expose which physical host a worker
landed on. `worker_id` is recorded per trial so repeat placement is *detectable*, but the
image-cache confound cannot be ruled out — only spread across arms by interleaving, which
the trial losses then partially undid. This is the largest remaining threat to the result.

**In-worker execution drifted upward during round 2** (14.5s to 19.5s across ten trials)
with no corresponding trend in startup. Unexplained. It is spread across both arms by the
counterbalanced order, so it does not bias the comparison, but it is why `wall` and
`delayTime` disagree in sign.

**The order effect is characterised, not explained.** That cold start falls 186s → 51s and
plateaus is measured. That the cause is host-level image caching is the **most likely
explanation given Phase 2's run-D finding, not a measurement.** Competing explanations —
registry-side caching, scheduler affinity to recently-used hosts, US-IL-1 warming for
unrelated reasons — were not ruled out.

**The Phase 2 comparison is not like-for-like.** These pin US-IL-1 and cap workers at 1;
Phase 2 did neither. Do not present these as a continuation of that series.

**Cost was not measured.** Phase 2 established that Runpod billing lags materially and
should not be quoted until the endpoint is deleted and usage settles. No per-job cost is
claimed. In particular, **whether FlashBoot changes billed worker seconds is unknown.**

**`PRIORITY_FLASHBOOT` was not tested.** The API exposes a third mode above `OFF` and
`FLASHBOOT`.

**Warm execution was not re-measured.** Nothing here updates Phase 2's 13.3s warm round
trip or the concurrency findings.

## What to measure next

1. **Control the order effect directly** — fire a fixed number of warming trials, confirm
   the plateau, *then* start the A/B from a known-warm pool. The 51s floor looks stable
   enough to be a usable baseline.
2. **Get n ≥ 5 per arm on `/run`**, not `/runsync`. The transport faults, not the cold
   starts, are what cost this phase its statistical power.
3. **Randomise the condition order** rather than strictly alternating, so a periodic
   external effect cannot align with the alternation.
4. **Then, and only then, quote a FlashBoot percentage.**
