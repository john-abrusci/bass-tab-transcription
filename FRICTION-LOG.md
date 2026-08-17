# Runpod Serverless — friction log

Notes from putting a real GPU workload on Runpod Serverless: audio source separation plus
pitch tracking, deployed, measured, and torn down. Every observation below came from using
the product, not from reading about it. Evidence is in [`PHASE2.md`](PHASE2.md); the raw
runs are in `bass-transcribe-worker/bench/results.jsonl`.

Ranked by what I would fix first, with the reasoning for the ranking, because the ranking is
the actual claim.

**Read the limits first.** This is n=1: one region, one GPU pool (`ADA_24`), one image, ~25
jobs over a few hours, by someone seeing the product for the first time. Small samples,
and some of what looks like a platform issue may be my network or my inexperience. Where I
could not isolate a cause, I say so.

---

## Ranking

| # | Friction | Why this rank | Cost to fix |
|---|---|---|---|
| 1 | Cold start is unknowable before you commit | Blocks the buy decision itself | Docs + console surface |
| 2 | `/runsync` is not synchronous | Cheap to fix, bit me in three places | Docs + naming |
| 3 | Base64 upload fails most first attempts | Documented path, high failure rate — but unisolated | Investigation first |
| 4 | Autoscaler config implies behaviour it does not deliver | Silent, and users will over-trust it | Observability |
| 5 | Availability signal did not predict capacity | Misleads GPU selection | Data accuracy |
| 6 | Billing lags and has no per-job granularity | Blocks unit-economics reasoning | Reporting |
| 7 | Endpoints pre-warm a worker on creation | Surprising, but harmless once known | Docs |

Two things worth saying that are not problems: the **pricing model is a genuine strength
that is under-sold** (see below), and the **agent onboarding is the best part of the
product I touched**.

---

## 1. Cold start is unknowable before you commit

**What I measured:** three independent scale-from-zero cold starts at **217.7s, 171.7s and
144.3s** on a 4.04 GiB image. About 86% of a cold request is worker startup; roughly 1% is
the inference the user asked for.

**Why it ranks first.** This is not "cold start is slow" — that is expected and fine. It is
that *the number is undiscoverable until you have built an image, deployed it, and measured
it yourself*. Anyone evaluating serverless GPU has to answer "will my users wait, and how
long?" before committing engineering time, and today the only way to find out is to do the
work first.

It is also the number that dominates everything else. It made the GPU tier comparison
pointless — only ~11.9s of a 13.3s warm round trip is GPU-bound, against a 145–220s cold
path — and it drives cost, because startup is billed worker time.

**The product question, which is a PM question not an engineering one:** *what number should
a user see, and where?* Candidates: a measured cold-start estimate per image size shown at
endpoint creation; a "time to first response" figure in the console after first deploy; or
guidance in the docs relating image size to expected pull time. Any of those turns a
three-hour discovery into a five-second one.

**What I got wrong here, which sharpens the point.** I first measured 15.2s and published a
finding that cold start was "not one number — 218s fresh, 15s cached," treating 15s as
typical. It was not a cold start at all: it was a redeploy landing on a host still holding
the previous image. I only caught it by re-measuring deliberately. If the product had
surfaced the number, I would not have had a wrong model to correct.

---

## 2. `/runsync` is not synchronous

**What happened:** `/runsync` waits a bounded window, then returns `{"status": "IN_QUEUE",
"id": ...}` and expects you to poll `/status`. With a 145–220s cold start, every cold
request exceeded that window.

**Why it matters more than it sounds.** The name and the docs' framing both say
"synchronous," so I wrote three separate call sites assuming it blocks to completion: the
benchmark harness, the web app, and a verification script. All three recorded cold starts as
failures. The app surfaced it to the user as an error. That is one wrong mental model
propagating into every layer of an integration.

**Cheapest fix on this list.** Docs stating the wait window explicitly, and ideally a
response field or header saying "this returned early, poll `/status`." A rename would be
better and is presumably off the table for compatibility.

**Adjacent:** the bounded window interacts badly with the cold start above. A synchronous
request/response shape does not fit a workload whose cold path is three minutes. Users on
scale-to-zero probably want `/run` plus polling by default, and the docs could say so.

---

## 3. Base64 upload fails most first attempts at song size

**What I measured:**

| Payload (base64) | First-attempt outcome |
|---|---|
| 0.75 MB | never observed to fail |
| 2.19 MB | 4 of 5 needed a retry |
| 3.28 MB | 3 of 4 failed outright |
| 11.11 MB | 5 of 5 failed |

Failures were `Broken pipe` and once `SSL_ALERT_BAD_RECORD_MAC` — a corrupted TLS record,
which is transport-level, not the API refusing anything.

**Why it is ranked third rather than first, despite the numbers.** *I could not isolate the
cause.* It may be my network path, my client, or an intermediary — not Runpod. A confident
bug report here would be overreach. What I can say is that the failure rate scales with
payload size with no clean cutoff, which rules out "request too large" as the model.

**The recommendation is therefore investigation, not a fix.** If this is reproducible from
other clients and networks, it is severe: a documented path failing 80% of first attempts at
a few MB. If it is not, the docs could still usefully state a recommended payload ceiling and
point at presigned URLs above it. I ended up transcoding to 1.6 MB to get reliable uploads.

---

## 4. Autoscaler config implies behaviour it does not deliver

**What I measured:** with `workersMax: 10`, ten simultaneous requests achieved **~1.5x
effective parallelism** — 158s of execution completing in a 105s window. p50 went 13.3s →
66.9s, p95 to 89.9s. Queue delay ran 39–73s against 150ms idle.

The autoscaler did fire — four jobs landed on brand-new workers. But new workers take
~15s+ to arrive even in the best case, and several pool workers were throttled throughout.
Scale-up was real and too late to matter.

**The friction is the mental model.** `workersMax: 10` reads as "ten at once." Nothing in
the config surface communicates that scale-up latency and pool capacity gate what you
actually get. A user sizing an endpoint for a traffic spike would get this wrong.

**What would help:** surfacing achieved concurrency versus configured maximum, and queue
wait as a first-class metric in the console. Runpod already returns `delayTime` separately
from `executionTime` per job, which is genuinely good — it is just not aggregated anywhere a
user would look.

---

## 5. The availability signal did not predict capacity

`list-gpu-types` reported `availability: HIGH` for `ADA_24` (RTX 4090). Across the session
that same endpoint showed **1 to 8 throttled workers**, continuously.

If `HIGH` means "you can deploy here" rather than "you will get capacity under load," that
is defensible but the distinction is invisible at the point of choosing a GPU. It directly
affects tier selection, which is one of the first decisions a new user makes.

---

## 6. Billing lags, and has no per-job granularity

**I reported the cost of this project wrong three times.** $0.030 total read mid-session,
then $0.479 with a guessed job count, before the settled figure of **$0.515 across 25 jobs —
~$0.021/job**. Each reading came from the billing API; the earlier ones were simply too
early.

Two distinct issues:

- **Lag.** Mid-session cost readings are not trustworthy, and nothing signals that. A user
  doing exactly what I did — checking spend while iterating — will draw wrong conclusions.
- **Granularity.** There is no per-job cost. I wanted to compare cold-job cost against
  warm-job cost, asserted a 13x spread from aggregates, and had to retract it when the
  per-endpoint split contradicted it. That question is unanswerable with the data exposed.

**Also worth surfacing: `idleTimeout` is a cost lever and is not framed as one.** I raised it
from 5s to 120s purely for measurement convenience, and it plausibly dominates the cost of
the busier endpoint, because workers sat alive-but-idle on the clock. Nothing in the setting's
presentation suggests a billing consequence.

---

## 7. Endpoints pre-warm a worker on creation

Creating an endpoint with `workersMin: 0` starts a worker anyway. Harmless in production —
arguably helpful — but it silently invalidates the first cold-start measurement anyone takes,
which is likely to be one of the first things a new user does. Worth a line in the docs.

---

## Not problems: two things that are good

**The pricing model is a genuine strength and is under-sold.** ~$0.021/job against roughly
$17.76/day for an always-warm 4090 puts the crossover near **850 jobs/day**. For bursty
workloads scale-to-zero wins by a wide margin, and I did not know that going in — I had
budgeted an order of magnitude more. That is a story worth telling prospective users
explicitly, with worked examples, rather than leaving them to derive it.

**Agent onboarding is the best part of the product I touched.** `docs.runpod.io/agent-setup.md`
installed skills plus an MCP server in two commands and worked first time. Auth is OAuth with
no API key to store. Control-plane operations — create endpoint, change GPU pool, read
billing, stream logs — were all available without ever minting a long-lived credential. That
is a materially better security posture than the usual "paste an API key," and it is a
differentiator worth being louder about.

---

## What I would not prioritise

**Making cold start faster on Runpod's side.** The 4.04 GiB image is mostly a CUDA base
layer I chose. That is my problem to fix, not the platform's. What the platform owes me is
*visibility* into the consequence, which is why #1 is about surfacing the number rather than
shrinking it.

**FlashBoot evaluation.** It exists and presumably addresses exactly this, but I measured
with it OFF throughout to get a clean baseline and never came back to it. Quantifying what it
buys is the obvious next experiment and I have no data on it — which is itself a small
finding, since I only discovered the setting by reading the API schema rather than from
anything that surfaced during setup.
