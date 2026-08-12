# Phase 2 — measurements

Every table below is blank. Nothing here is an estimate; fill it in from
`bass-transcribe-worker/bench/results.jsonl` via `python bench/measure.py report`.

Test track: _(name, length, genre — use the same one for every row or the comparisons mean
nothing)_

---

## Preliminary: synthetic 3.2s clip, not the real track

These are from `tools/make_test_tone.py` — four open-string notes, 3.2s — used to verify
the endpoint end to end. **Separation time scales with audio length, so none of these
numbers transfer to a 4-minute track.** They are here for the cold-start finding only.

Endpoint `v22yhtuixccxc0`, RTX 4090 (`ADA_24`), FlashBoot OFF, weights baked in,
`workersMin` 0, `idleTimeout` 5s.

| Run | Image state on host | delayTime | exec | model load | separation | pitch |
|---|---|---|---|---|---|---|
| 1 | never pulled anywhere | **217.7s** | 21.8s | 1.52s | 3.15s | 16.93s |
| 2 | same image, new worker | — | — | 0.66s | 1.47s | 14.9s |
| 3 | new tag, 12/13 layers cached | **15.2s** | 19.9s | 2.07s | 1.60s | 15.76s |

**Cold start is not one number.** A host that has never seen the image pays ~218s to pull
4.1 GiB. A host that already has the base layers pays ~15s, because a code-only rebuild
changes just the final `COPY` layer. Same endpoint, same scale-to-zero state, 14x apart.

Two consequences worth carrying into the real measurements:

- Quoting a single cold-start figure is misleading. Report both, and say which one a user
  actually experiences — that depends on how often Runpod schedules you onto a fresh host,
  which is itself worth measuring.
- It weakens the case for the network-volume experiment. If layer caching already gets the
  common case to 15s, moving weights off the image mostly helps the rare first pull. Worth
  measuring anyway, but predict the result before running it.

**`pitch_s` is ~15s on a 3.2s clip and barely moves with audio length** — that is
torchcrepe loading its weights on first call, not inference. Every run above hit a cold
worker (`idleTimeout: 5` scales down almost instantly), so this cost appears every time.
On a warm worker it should collapse. Measuring that requires raising `idleTimeout` first —
which is also why there are no warm numbers here yet.

---

## Cold start

Zero warm workers. `python bench/measure.py cold --audio track.mp3 --label "..."`

| Config | Wall (request → response) | Container pull + boot | Model load | Separation | Pitch |
|---|---|---|---|---|---|
| Weights baked into image | | | | | |
| Weights on network volume | | | | | |

Container pull + boot is `wall − total_s − (queue wait)`; `worker_uptime_at_request_s` in
the response tells you how long the process had been alive when the request landed, which
splits pull from load.

**Which is faster, and by how much?**

**Why:** _(image pull is one big sequential read; a volume is a smaller image but a slower
random read. Say which dominated here.)_

---

## Warm execution

3–4 minute track, worker already up. `python bench/measure.py warm --n 5`

| | Median | p95 |
|---|---|---|
| Separation | | |
| Pitch tracking | | |
| Worker total | | |
| Round trip incl. payload | | |
| Payload + queue overhead | | |

**Is payload transfer a meaningful share of latency?** If yes, switch to presigned-URL
upload — the worker already accepts `audio_url`.

---

## GPU selection

Same 4-minute track on each tier.

| GPU | Separation | Pitch | Total | Cost/hr | Cost/job | Notes |
|---|---|---|---|---|---|---|
| RTX A4000 / A5000 | | | | | | cheapest viable |
| RTX 4090 / L40S | | | | | | mid |
| A100 / H100 | | | | | | almost certainly overkill |

**At what job volume does a faster, pricier GPU become the cheaper choice per job?**

Cost per job is `(cost/hr ÷ 3600) × billed seconds`, and billed seconds includes cold start
on a scale-to-zero endpoint. So the crossover depends on the cold-start share, not just on
inference speed: a faster GPU that still pays the same 30s pull wins less than its
throughput suggests.

For a bursty consumer workload the answer is usually "never" — derive it rather than
assuming it.

**Answer:**

---

## Scale-to-zero vs. always warm

| | Value |
|---|---|
| Cost of one always-warm worker, 24h idle | |
| Cold start penalty per job | |
| Jobs/day at which a warm worker pays for itself | |

**Which would you ship, and why?**

---

## Concurrency

`python bench/measure.py burst --n 10`

| Concurrent requests | p50 | p95 | Batch wall | Failures |
|---|---|---|---|---|
| 1 | | | | |
| 5 | | | | |
| 10 | | | | |

**Does scale-up keep pace, or do requests queue?** Compare p95 against the single-request
number: if p95 ≈ p50 the endpoint scaled; if p95 ≈ p50 × N it serialised. Runpod reports
`delayTime` separately from `executionTime`, and `measure.py` records both — queue wait
shows up in `delayTime`.

**Observation:**

---

## Conclusions

Three or four sentences. What surprised you, what you would change, and what you would
ship.
