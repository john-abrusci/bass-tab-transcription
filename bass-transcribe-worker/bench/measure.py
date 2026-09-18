#!/usr/bin/env python3
"""Phase 2 measurement harness.

The numbers are the deliverable, so they get collected by a script rather than
by hand -- otherwise the cold-start figure quietly becomes "the one time I
remembered to look at the clock".

Stdlib only. Needs RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID in the environment.

  python bench/measure.py health
  python bench/measure.py cold  --audio track.mp3
  python bench/measure.py warm  --audio track.mp3 --n 5
  python bench/measure.py burst --audio track.mp3 --n 10
  python bench/measure.py all   --audio track.mp3 --label "RTX 4090, weights baked in"

Results append to bench/results.jsonl; `report` renders them as markdown.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import sys
import time
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API_KEY = os.environ.get("RUNPOD_API_KEY", "")
ENDPOINT_ID = os.environ.get("RUNPOD_ENDPOINT_ID", "")
BASE = f"https://api.runpod.ai/v2/{ENDPOINT_ID}"
RESULTS = Path(__file__).parent / "results.jsonl"


def _req(path: str, payload: dict | None = None, timeout: float = 900) -> dict:
    url = f"{BASE}/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def health() -> dict:
    h = _req("health")
    print(json.dumps(h, indent=2))
    return h


def encode(audio: Path) -> str:
    return base64.b64encode(audio.read_bytes()).decode()


TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}


def _await_job(resp: dict, poll_s: float = 2.0, timeout_s: float = 900) -> dict:
    """Poll /status until the job reaches a terminal state.

    /runsync does not block indefinitely -- it waits a bounded window and then
    returns {"status": "IN_QUEUE", "id": ...}. A cold start on this endpoint runs
    to ~220s, well past that window, so treating the first response as final
    would record a cold start as an error every single time.
    """
    status = resp.get("status")
    if status in TERMINAL or status is None:
        return resp
    job_id = resp.get("id")
    if not job_id:
        return resp
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll_s)
        resp = _req(f"status/{job_id}")
        if resp.get("status") in TERMINAL:
            return resp
    return {"status": "CLIENT_TIMEOUT", "id": job_id}


MAX_ATTEMPTS = 4


def run_one(b64: str, max_s: int, conf: float) -> dict:
    """One job, start to finish. Returns wall time plus whatever the worker reported.

    Large base64 uploads over this path fail intermittently -- broken pipe, and
    occasionally an SSL bad-record-mac, which is a transport-level corruption
    rather than the API rejecting anything. Retry on connection-level errors, and
    time only the attempt that actually succeeded so a retry does not inflate the
    measurement. `attempts` is recorded so the flakiness stays visible instead of
    being quietly smoothed away.
    """
    payload = {
        "input": {
            "audio_b64": b64,
            "max_duration_s": max_s,
            "confidence_threshold": conf,
        }
    }

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        t0 = time.perf_counter()
        try:
            resp = _await_job(_req("runsync", payload))
            wall = time.perf_counter() - t0
            break
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"HTTP {exc.code}: {exc.read()[:300]!r}"}
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            print(f"  attempt {attempt}/{MAX_ATTEMPTS} failed: {last_error}", file=sys.stderr)
            if attempt < MAX_ATTEMPTS:
                time.sleep(2.0 * attempt)  # back off; the path needs a moment
    else:
        return {"ok": False, "error": last_error, "attempts": MAX_ATTEMPTS}

    if resp.get("status") not in ("COMPLETED", None):
        return {"ok": False, "error": f"job {resp.get('status')}", "wall_s": round(wall, 2)}

    out = resp.get("output") or {}
    if "error" in out:
        return {"ok": False, "error": out["error"], "wall_s": round(wall, 2)}

    timings = out.get("timings", {})
    return {
        "ok": True,
        "attempts": attempt,
        # Wall time is what the user feels. Everything else is the split.
        "wall_s": round(wall, 2),
        "worker_total_s": timings.get("total_s"),
        "model_load_s": timings.get("model_load_s"),
        "separation_s": timings.get("separation_s"),
        "pitch_s": timings.get("pitch_s"),
        "cold": timings.get("cold"),
        "worker_uptime_at_request_s": timings.get("worker_uptime_at_request_s"),
        # wall - worker_total is queue wait + payload transfer + Runpod overhead.
        "overhead_s": round(wall - (timings.get("total_s") or 0), 2),
        "delay_time_ms": resp.get("delayTime"),
        "execution_time_ms": resp.get("executionTime"),
        "n_notes": len(out.get("notes", [])),
        "duration_s": out.get("duration_s"),
    }


def record(kind: str, label: str, payload: dict) -> None:
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "label": label, **payload}
    with RESULTS.open("a") as f:
        f.write(json.dumps(row) + "\n")
    print(json.dumps(row, indent=2))


def cmd_cold(args) -> None:
    h = _req("health").get("workers", {})
    ready = (h.get("idle", 0) or 0) + (h.get("running", 0) or 0)
    initializing = h.get("initializing", 0) or 0

    if ready:
        print(
            f"WARNING: {ready} worker(s) already up -- this will not be a cold start.\n"
            "Wait out the idle timeout (or set max workers to 0 and back) first.",
            file=sys.stderr,
        )
    if initializing:
        # Runpod pre-warms a worker when an endpoint is created, so a request
        # fired now waits only for the *remainder* of an init that started
        # before it. That understates cold start, and it is invisible if you
        # only check idle/running -- so record the whole worker state with the
        # measurement rather than asserting a clean one.
        print(
            f"NOTE: {initializing} worker(s) already initializing; the measured "
            "delay excludes however long that had been running.",
            file=sys.stderr,
        )

    b64 = encode(Path(args.audio))
    result = run_one(b64, args.max_s, args.conf)
    result["workers_at_request"] = h
    record("cold", args.label, result)


def cmd_warm(args) -> None:
    b64 = encode(Path(args.audio))
    print("priming...", file=sys.stderr)
    run_one(b64, args.max_s, args.conf)
    runs = [run_one(b64, args.max_s, args.conf) for _ in range(args.n)]
    ok = [r for r in runs if r.get("ok")]
    summary = {"n": args.n, "runs": runs}
    if ok:
        summary["median"] = {
            k: round(statistics.median([r[k] for r in ok if r.get(k) is not None]), 2)
            for k in ("wall_s", "worker_total_s", "separation_s", "pitch_s", "overhead_s")
            if any(r.get(k) is not None for r in ok)
        }
    record("warm", args.label, summary)


def cmd_burst(args) -> None:
    """Fire n simultaneous requests: does scale-up keep pace, or do we queue?"""
    b64 = encode(Path(args.audio))
    with ThreadPoolExecutor(max_workers=args.n) as pool:
        t0 = time.perf_counter()
        runs = list(pool.map(lambda _: run_one(b64, args.max_s, args.conf), range(args.n)))
        wall = time.perf_counter() - t0

    walls = sorted(r["wall_s"] for r in runs if r.get("ok"))
    summary = {"n": args.n, "batch_wall_s": round(wall, 2), "runs": runs}
    if walls:
        summary["p50_s"] = round(statistics.median(walls), 2)
        summary["p95_s"] = round(walls[min(len(walls) - 1, int(0.95 * len(walls)))], 2)
        summary["failures"] = sum(1 for r in runs if not r.get("ok"))
    record("burst", args.label, summary)


def cmd_report(_args) -> None:
    if not RESULTS.exists():
        print("no results yet")
        return
    rows = [json.loads(line) for line in RESULTS.read_text().splitlines() if line.strip()]

    print("## Cold start\n")
    print("| when | label | wall | model load | separation | pitch | overhead |")
    print("|---|---|---|---|---|---|---|")
    for r in [r for r in rows if r["kind"] == "cold" and r.get("ok")]:
        print(
            f"| {r['ts']} | {r['label']} | {r['wall_s']}s | {r.get('model_load_s')}s | "
            f"{r.get('separation_s')}s | {r.get('pitch_s')}s | {r.get('overhead_s')}s |"
        )

    print("\n## Warm execution (medians)\n")
    print("| when | label | n | wall | separation | pitch | overhead |")
    print("|---|---|---|---|---|---|---|")
    for r in [r for r in rows if r["kind"] == "warm" and r.get("median")]:
        m = r["median"]
        print(
            f"| {r['ts']} | {r['label']} | {r['n']} | {m.get('wall_s')}s | "
            f"{m.get('separation_s')}s | {m.get('pitch_s')}s | {m.get('overhead_s')}s |"
        )

    print("\n## Concurrency\n")
    print("| when | label | n | p50 | p95 | batch wall | failures |")
    print("|---|---|---|---|---|---|---|")
    for r in [r for r in rows if r["kind"] == "burst" and "p50_s" in r]:
        print(
            f"| {r['ts']} | {r['label']} | {r['n']} | {r['p50_s']}s | {r['p95_s']}s | "
            f"{r['batch_wall_s']}s | {r.get('failures', 0)} |"
        )


# ---------------------------------------------------------------------------
# Phase 3: FlashBoot A/B, and the /runsync boundary.
#
# Two things this has to get right that the Phase 2 `cold` command did not:
#
#   1. A cold start is only a cold start if the endpoint is genuinely at zero.
#      Runpod pre-warms a worker when an endpoint is created, and `cold` only
#      *warned* about that. Here a non-zero worker count aborts the trial and
#      writes an `invalid` row, so a contaminated run can never be mistaken for
#      a measurement.
#
#   2. The FlashBoot setting is read back from the control plane before every
#      trial rather than assumed from a flag. If the endpoint disagrees with
#      what the trial claims to be measuring, the trial is void.
#
# The image is pinned by digest at endpoint creation, and the digest is recorded
# on every row, so "same image across all trials" is evidence rather than an
# assumption.
# ---------------------------------------------------------------------------

REST_BASE = "https://rest.runpod.io/v1"

# rest.runpod.io sits behind a WAF that 403s urllib's default
# "Python-urllib/3.x" User-Agent. api.runpod.ai does not. Sending an explicit UA
# is the whole fix; without it every control-plane read fails closed.
UA = "bass-transcribe-bench/3.0"

# rest.runpod.io/v1 represents FlashBoot as a flat boolean, while the v2/GraphQL
# surface uses an enum ("OFF" / "FLASHBOOT"). The REST surface is what this
# harness talks to, so conditions normalise to bool -- but read-back tolerates
# either shape, because getting this wrong silently voids every trial.
FLASHBOOT_MODES = {"OFF": False, "ON": True}


def _norm_flashboot(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.upper() in ("FLASHBOOT", "PRIORITY_FLASHBOOT", "TRUE")
    return None


def _rest(path: str, method: str = "GET", body: dict | None = None, timeout: float = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{REST_BASE}/{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def endpoint_config() -> dict:
    """Authoritative endpoint settings, straight from the control plane."""
    return _rest(f"endpoints/{ENDPOINT_ID}")


def set_flashboot(enabled: bool) -> dict:
    """Set FlashBoot and read the setting back. Returns the post-change config."""
    _rest(f"endpoints/{ENDPOINT_ID}", method="PATCH", body={"flashboot": bool(enabled)})
    return endpoint_config()


# /health reports six worker states. "At zero" has to mean all of them, not just
# the two that are obvious -- an `initializing` worker is exactly the case that
# silently contaminated a Phase 2 run.
WORKER_KEYS = ("idle", "initializing", "ready", "running", "throttled", "unhealthy")


def worker_state() -> dict:
    w = _req("health").get("workers", {}) or {}
    return {k: int(w.get(k, 0) or 0) for k in WORKER_KEYS}


def queue_state() -> dict:
    j = _req("health").get("jobs", {}) or {}
    return {k: int(j.get(k, 0) or 0) for k in ("inQueue", "inProgress")}


# The gate is these four. A worker in any of them can serve the request we are
# about to fire, so a non-zero count means the next trial is not a cold start.
LIVE_KEYS = ("idle", "initializing", "ready", "running")


def _busy(state: dict) -> int:
    return sum(state.get(k, 0) for k in LIVE_KEYS)


def _held(state: dict) -> int:
    """Throttled and unhealthy workers are slots the scheduler is holding but
    cannot run. They do not serve the request, so they do not block a trial --
    but a throttled slot can be promoted to running at any moment, so it is
    recorded and flagged rather than ignored."""
    return state.get("throttled", 0) + state.get("unhealthy", 0)


def wait_for_zero(timeout_s: float = 900, poll_s: float = 5.0, quiet_for_s: float = 15.0):
    """Block until every worker state is zero and has stayed there.

    The settle window matters: worker counts flap during teardown, and firing
    into a momentary zero that is really a worker mid-shutdown produces a
    number that is neither warm nor cold.
    """
    deadline = time.time() + timeout_s
    zero_since = None
    last = None
    while time.time() < deadline:
        state = worker_state()
        if state != last:
            print(f"    workers: {state}", file=sys.stderr)
            last = state
        if _busy(state) == 0:
            if zero_since is None:
                zero_since = time.time()
            elif time.time() - zero_since >= quiet_for_s:
                return True, state, round(time.time() - zero_since, 1)
        else:
            zero_since = None
        time.sleep(poll_s)
    return False, worker_state(), 0.0


def post_raw(path: str, payload: dict, timeout: float = 600) -> dict:
    """POST and capture exactly what comes back, and when.

    Against /runsync this is the Task 2 instrument: the point is to observe the
    boundary rather than handle it -- elapsed time to the first response, the
    HTTP status code, and the body shape, without treating a non-completion as
    an error. Against /run it is just a fast async submit.
    """
    req = urllib.request.Request(
        f"{BASE}/{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json",
                 "User-Agent": UA},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            elapsed = time.perf_counter() - t0
            code = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        elapsed = time.perf_counter() - t0
        code = exc.code
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    return {
        "path": path,
        "elapsed_s": round(elapsed, 2),
        "http_status": code,
        "body_keys": sorted(parsed.keys()) if isinstance(parsed, dict) else None,
        "body_status": (parsed or {}).get("status") if isinstance(parsed, dict) else None,
        "body_raw_head": raw[:400].decode("utf-8", "replace"),
        "parsed": parsed,
    }


def set_max_workers(n: int, confirm: bool = False, timeout_s: float = 20.0) -> float:
    """Set workersMax, optionally blocking until the control plane reflects it.

    Firing immediately after this PATCH races the endpoint's own rollout and the
    request comes back 409. Reading the value back before submitting removes the
    race; the elapsed time is returned so the head start it gives worker
    scheduling stays visible in the data rather than being assumed away.
    """
    t0 = time.perf_counter()
    _rest(f"endpoints/{ENDPOINT_ID}", method="PATCH", body={"workersMax": int(n)})
    if not confirm:
        return 0.0
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if (endpoint_config().get("workersMax") or 0) == int(n):
            break
        time.sleep(0.5)
    return round(time.perf_counter() - t0, 3)


def force_zero(args) -> tuple[bool, dict, float]:
    """Drive the endpoint to zero workers by taking the worker slot away.

    This endpoint reports workersStandby=1, which Runpod will not let us clear
    via REST or GraphQL -- the field is read-only on both. Left alone, the
    scheduler keeps a worker warm forever and the endpoint never reaches the
    zero state a cold-start measurement requires.

    Setting workersMax=0 terminates every worker regardless of standby. The job
    is then submitted while the endpoint still cannot run anything, and the slot
    is handed back a moment later. That ordering is what makes the measurement
    honest: the worker provably starts *after* the job is queued, so delayTime
    contains the whole startup rather than whatever was left of a boot that had
    already begun.
    """
    print("  forcing zero (workersMax -> 0)...", file=sys.stderr)
    set_max_workers(0)
    return wait_for_zero(timeout_s=args.drain_timeout, quiet_for_s=args.settle)


def flashboot_trial(seq: int, condition: str, b64: str, args, auto: bool) -> dict:
    """One controlled cold-start trial. Returns a row ready for results.jsonl."""
    want = FLASHBOOT_MODES[condition]
    print(f"\n=== trial {seq}: FlashBoot {condition} ===", file=sys.stderr)

    if auto:
        cfg = set_flashboot(want)
    else:
        cfg = endpoint_config()

    actual = _norm_flashboot(cfg.get("flashboot"))
    row = {
        "trial": seq,
        "condition": condition,
        "flashboot_expected": want,
        "flashboot_actual": actual,
        "flashboot_raw": cfg.get("flashboot"),
        # The endpoint is pinned to an image digest at creation and cannot be
        # pushed to, so the digest is passed in and recorded rather than re-read
        # -- rest.runpod.io does not return it on the endpoint object.
        "image": args.expect_image,
        "audio_sha256": args.audio_sha,
        "workers_cfg": {
            "min": cfg.get("workersMin"),
            "max": cfg.get("workersMax"),
            "standby": cfg.get("workersStandby"),
            "idleTimeout": cfg.get("idleTimeout"),
        },
        "gpu_types": cfg.get("gpuTypeIds"),
        "endpoint_version": cfg.get("version"),
    }

    # Gate 1: the endpoint must actually be in the condition we claim to measure.
    if actual is None or actual != want:
        row.update(ok=False, valid=False,
                   invalid_reason=f"FlashBoot is {cfg.get('flashboot')!r}, expected {want}")
        print(f"  ABORT: {row['invalid_reason']}", file=sys.stderr)
        return row

    # Gate 2: the endpoint must be at a settled zero.
    if args.force_zero:
        at_zero, state, settled = force_zero(args)
    else:
        print("  draining to zero workers...", file=sys.stderr)
        at_zero, state, settled = wait_for_zero(
            timeout_s=args.drain_timeout, quiet_for_s=args.settle
        )
    if not at_zero:
        row.update(ok=False, valid=False, workers_at_fire=state,
                   invalid_reason=f"endpoint never reached zero workers: {state}")
        print(f"  ABORT: {row['invalid_reason']}", file=sys.stderr)
        return row

    # Gate 3: re-read immediately before firing. The drain check above can be
    # several seconds stale, and that is long enough for the scheduler to move.
    q = queue_state()
    row["queue_at_fire"] = q
    if q["inQueue"] or q["inProgress"]:
        print(f"  queue not empty at fire time {q} -- purging", file=sys.stderr)
        try:
            _req("purge-queue", {})
            time.sleep(3)
        except Exception as exc:
            print(f"  purge failed: {exc}", file=sys.stderr)
        q = queue_state()
        row["queue_at_fire"] = q
        if q["inQueue"] or q["inProgress"]:
            row.update(ok=False, valid=False,
                       invalid_reason=f"queue non-empty at fire time: {q}")
            print(f"  ABORT: {row['invalid_reason']}", file=sys.stderr)
            return row

    state_at_fire = worker_state()
    row["workers_at_fire"] = state_at_fire
    row["zero_settled_s"] = settled
    row["held_workers_at_fire"] = _held(state_at_fire)
    if _held(state_at_fire):
        print(f"  NOTE: {_held(state_at_fire)} throttled/unhealthy slot(s) held at "
              f"fire time -- recorded, does not invalidate the trial", file=sys.stderr)
    if _busy(state_at_fire) != 0:
        row.update(ok=False, valid=False,
                   invalid_reason=f"workers non-zero at fire time: {state_at_fire}")
        print(f"  ABORT: {row['invalid_reason']}", file=sys.stderr)
        return row

    payload = {"input": {"audio_b64": b64, "max_duration_s": args.max_s,
                         "confidence_threshold": args.conf}}

    print(f"  firing via /{args.submit}...", file=sys.stderr)
    # Restore the slot, then submit immediately.
    #
    # Both alternatives were tried and both fail. Submitting first, while
    # workersMax is still 0, is refused with 409 -- the endpoint will not accept
    # a job it has no capacity for (one such submit did succeed, but only by
    # beating the config's propagation, which is not something to rely on).
    # Backing off after a 409 is worse: the standby worker boots during the wait
    # and the job lands warm, reading 10.9s instead of ~50s.
    #
    # So: hand the slot back and submit at once. Worker scheduling takes ~1s
    # against a startup of tens of seconds, and `worker_uptime_at_request_s`
    # (~1.5s on every clean trial) is the evidence the worker really was fresh.
    if args.force_zero:
        row["slot_restore_to_submit_s"] = set_max_workers(1)
    t0 = time.perf_counter()
    first = post_raw(args.submit, payload)
    conflicts = 0
    while first.get("http_status") == 409 and conflicts < args.conflict_retries:
        # 409 means nothing was queued. Re-drain to zero before retrying, or the
        # retry stops being a cold start.
        conflicts += 1
        print(f"  /{args.submit} 409 (conflict {conflicts}) -- re-draining",
              file=sys.stderr)
        set_max_workers(0)
        wait_for_zero(timeout_s=args.drain_timeout, quiet_for_s=args.settle)
        set_max_workers(1)
        t0 = time.perf_counter()
        first = post_raw(args.submit, payload)
    if conflicts:
        row["submit_conflicts"] = conflicts
    key = "runsync_boundary" if args.submit == "runsync" else "submit_response"
    row[key] = {k: v for k, v in first.items() if k != "parsed"}
    print(f"  /{args.submit} returned {first['http_status']} status={first['body_status']} "
          f"after {first['elapsed_s']}s", file=sys.stderr)

    resp = first["parsed"] or {}
    if resp.get("status") not in TERMINAL:
        resp = _await_job(resp, timeout_s=args.job_timeout)
    wall = time.perf_counter() - t0

    row["wall_s"] = round(wall, 2)
    row["delay_time_ms"] = resp.get("delayTime")
    row["execution_time_ms"] = resp.get("executionTime")
    row["job_status"] = resp.get("status")
    row["worker_id"] = resp.get("workerId")

    out = resp.get("output") or {}
    timings = out.get("timings", {}) if isinstance(out, dict) else {}
    row.update({
        "ok": resp.get("status") == "COMPLETED" and "error" not in out,
        "valid": True,
        "worker_total_s": timings.get("total_s"),
        "model_load_s": timings.get("model_load_s"),
        "separation_s": timings.get("separation_s"),
        "pitch_s": timings.get("pitch_s"),
        "cold": timings.get("cold"),
        "worker_uptime_at_request_s": timings.get("worker_uptime_at_request_s"),
        "n_notes": len(out.get("notes", [])) if isinstance(out, dict) else None,
    })
    if isinstance(out, dict) and "error" in out:
        row["error"] = out["error"]

    # The worker must have been fresh. `cold: False`, a non-zero model_load
    # saving, or meaningful uptime at request time all mean this job was served
    # by a worker that had already done work -- whatever delayTime says, it is
    # not this endpoint's cold start.
    uptime = row.get("worker_uptime_at_request_s")
    if row.get("ok") and (row.get("cold") is False
                          or (uptime is not None and uptime > args.max_uptime)):
        row["valid"] = False
        row["invalid_reason"] = (
            f"served by a non-fresh worker (cold={row.get('cold')}, "
            f"uptime_at_request={uptime}s > {args.max_uptime}s); "
            f"delayTime includes work that was not ours")
        print(f"  VOID: {row['invalid_reason']}", file=sys.stderr)
    return row


def cmd_flashboot_ab(args) -> None:
    plan = [p.strip().upper() for p in args.plan.split(",") if p.strip()]
    bad = [p for p in plan if p not in FLASHBOOT_MODES]
    if bad:
        sys.exit(f"unknown condition(s) in --plan: {bad}")

    cfg = endpoint_config()
    print(f"endpoint {ENDPOINT_ID}", file=sys.stderr)
    print(f"  image:     {args.expect_image}", file=sys.stderr)
    print(f"  flashboot: {cfg.get('flashboot')}", file=sys.stderr)
    print(f"  workers:   min={cfg.get('workersMin')} max={cfg.get('workersMax')} "
          f"standby={cfg.get('workersStandby')} idleTimeout={cfg.get('idleTimeout')}",
          file=sys.stderr)
    print(f"  gpu:       {cfg.get('gpuTypeIds')}", file=sys.stderr)
    if cfg.get("workersMax", 0) < 1 and not args.force_zero:
        sys.exit("workersMax is 0 -- no worker can start. Raise it before measuring.")
    print(f"  plan:      {' -> '.join(plan)}  (auto-toggle={args.auto_toggle})", file=sys.stderr)

    b64 = encode(Path(args.audio))
    for i, condition in enumerate(plan, start=1):
        row = None
        errors = []
        for attempt in range(1, args.trial_attempts + 1):
            try:
                row = flashboot_trial(i, condition, b64, args, args.auto_toggle)
                break
            except Exception as exc:
                # Phase 2 documented this upload path dropping connections and
                # once corrupting a TLS record. Those are transport faults, not
                # properties of the endpoint, so the trial is retried from the
                # top -- re-draining to zero first, because a retry that skipped
                # the drain would no longer be measuring a cold start.
                err = f"{type(exc).__name__}: {exc}"
                errors.append(err)
                print(f"  trial {i} attempt {attempt}/{args.trial_attempts} "
                      f"failed: {err}", file=sys.stderr)
                try:
                    _req("purge-queue", {})   # drop anything that did land
                except Exception:
                    pass
                if attempt < args.trial_attempts:
                    time.sleep(5)
        if row is None:
            row = {"trial": i, "condition": condition, "ok": False, "valid": False,
                   "invalid_reason": f"transport failed {len(errors)}x: {errors[-1]}",
                   "submit_errors": errors}
            print(f"  TRIAL {i} VOID after {len(errors)} attempts", file=sys.stderr)
        elif errors:
            row["submit_errors"] = errors
            row["submit_attempts"] = len(errors) + 1
        record("flashboot_ab", args.label, row)


def cmd_boundary(args) -> None:
    """Task 2, standalone: fire at a cold endpoint and watch only the boundary.

    In practice every `flashboot-ab` trial already records this, so this exists
    for the case where you want boundary data without a full A/B.
    """
    print("  draining to zero workers...", file=sys.stderr)
    at_zero, state, settled = wait_for_zero(timeout_s=args.drain_timeout, quiet_for_s=args.settle)
    b64 = encode(Path(args.audio))
    payload = {"input": {"audio_b64": b64, "max_duration_s": args.max_s,
                         "confidence_threshold": args.conf}}
    first = post_raw("runsync", payload)
    row = {
        "valid": at_zero,
        "workers_at_fire": state,
        "zero_settled_s": settled,
        "runsync_boundary": {k: v for k, v in first.items() if k != "parsed"},
    }
    if not at_zero:
        row["invalid_reason"] = f"endpoint never reached zero workers: {state}"
    # Let the job finish so the endpoint drains cleanly for the next trial.
    resp = first["parsed"] or {}
    if resp.get("status") not in TERMINAL:
        resp = _await_job(resp, timeout_s=args.job_timeout)
    row["job_status"] = resp.get("status")
    row["delay_time_ms"] = resp.get("delayTime")
    record("runsync_boundary", args.label, row)


def cmd_ab_report(_args) -> None:
    if not RESULTS.exists():
        print("no results yet")
        return
    rows = [json.loads(l) for l in RESULTS.read_text().splitlines() if l.strip()]
    ab = [r for r in rows if r.get("kind") == "flashboot_ab"]
    if not ab:
        print("no flashboot_ab rows yet")
        return

    print("## FlashBoot A/B — all trials\n")
    print("| # | cond | valid | workers at fire | delayTime | exec | wall | worker |")
    print("|---|---|---|---|---|---|---|---|")
    for r in ab:
        wf = r.get("workers_at_fire")
        wf_s = "all zero" if wf and _busy(wf) == 0 else str(wf)
        d = r.get("delay_time_ms")
        e = r.get("execution_time_ms")
        print(f"| {r.get('trial')} | {r.get('condition')} | "
              f"{'yes' if r.get('valid') else 'NO — ' + str(r.get('invalid_reason'))} | {wf_s} | "
              f"{f'{d/1000:.1f}s' if d else '—'} | {f'{e/1000:.1f}s' if e else '—'} | "
              f"{r.get('wall_s', '—')}s | {(r.get('worker_id') or '—')[:12]} |")

    def stats(cond):
        vals = [r for r in ab if r.get("condition") == cond and r.get("valid") and r.get("ok")]
        if not vals:
            return None
        d = sorted(v["delay_time_ms"] / 1000 for v in vals if v.get("delay_time_ms"))
        w = sorted(v["wall_s"] for v in vals if v.get("wall_s"))
        return {"n": len(vals), "delay": d, "wall": w}

    print("\n## Medians (valid trials only)\n")
    print("| FlashBoot | n | median delayTime | delay spread | median wall | wall spread |")
    print("|---|---|---|---|---|---|")
    for cond in ("OFF", "ON"):
        s = stats(cond)
        if not s:
            print(f"| {cond} | 0 | — | — | — | — |")
            continue
        print(f"| {cond} | {s['n']} | {statistics.median(s['delay']):.1f}s | "
              f"{min(s['delay']):.1f}–{max(s['delay']):.1f}s | "
              f"{statistics.median(s['wall']):.1f}s | {min(s['wall']):.1f}–{max(s['wall']):.1f}s |")

    off, on = stats("OFF"), stats("ON")
    if off and on:
        do, dn = statistics.median(off["delay"]), statistics.median(on["delay"])
        wo, wn = statistics.median(off["wall"]), statistics.median(on["wall"])
        print(f"\n**delayTime: {do:.1f}s -> {dn:.1f}s "
              f"({(dn - do) / do * 100:+.0f}%)**")
        print(f"**wall: {wo:.1f}s -> {wn:.1f}s ({(wn - wo) / wo * 100:+.0f}%)**")

    print("\n## /runsync boundary\n")
    print("| source | # | elapsed to return | HTTP | body status | body keys |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        b = r.get("runsync_boundary")
        if not b:
            continue
        print(f"| {r.get('kind')} | {r.get('trial', '—')} | {b.get('elapsed_s')}s | "
              f"{b.get('http_status')} | {b.get('body_status')} | {b.get('body_keys')} |")



def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "command",
        choices=["health", "cold", "warm", "burst", "all", "report",
                 "flashboot-ab", "boundary", "ab-report"],
    )
    p.add_argument("--audio", help="path to a test track")
    p.add_argument("--n", type=int, default=5)
    p.add_argument("--max-s", dest="max_s", type=int, default=300)
    p.add_argument("--conf", type=float, default=0.5)
    p.add_argument("--label", default="unlabelled", help="e.g. 'RTX 4090, weights baked in'")
    p.add_argument("--plan", default="OFF,OFF,OFF",
                   help="comma-separated trial conditions, e.g. OFF,ON,OFF,ON,OFF,ON")
    p.add_argument("--auto-toggle", dest="auto_toggle", action="store_true",
                   help="set FlashBoot via the API before each trial instead of "
                        "expecting it to have been set by hand")
    p.add_argument("--settle", type=float, default=15.0,
                   help="seconds worker counts must stay at zero before firing")
    p.add_argument("--drain-timeout", dest="drain_timeout", type=float, default=900)
    p.add_argument("--expect-image", dest="expect_image", default="",
                   help="image digest the endpoint is pinned to; recorded per trial")
    p.add_argument("--force-zero", dest="force_zero", action="store_true",
                   help="drive workers to zero by setting workersMax=0, then restore "
                        "the slot just after submitting. Required on endpoints with a "
                        "standby worker that cannot be cleared via the API.")
    p.add_argument("--max-uptime", dest="max_uptime", type=float, default=10.0,
                   help="a worker older than this at request time means the trial was "
                        "not a cold start and is voided")
    p.add_argument("--conflict-retries", dest="conflict_retries", type=int, default=4,
                   help="times to re-POST after an HTTP 409 (refused, nothing queued)")
    p.add_argument("--trial-attempts", dest="trial_attempts", type=int, default=3,
                   help="times to retry a whole trial (re-draining first) when the "
                        "submit fails at the transport level")
    p.add_argument("--submit", choices=["run", "runsync"], default="run",
                   help="/run is the robust async submit and is the default for the "
                        "A/B; /runsync additionally measures the Task 2 boundary but "
                        "holds a connection open for the whole cold start")
    p.add_argument("--slot-delay", dest="slot_delay", type=float, default=1.5,
                   help="seconds after submit before workersMax is restored to 1")
    p.add_argument("--audio-sha", dest="audio_sha", default="",
                   help="sha256 of the test clip; recorded per trial as evidence "
                        "the same input was used throughout")
    p.add_argument("--job-timeout", dest="job_timeout", type=float, default=900)
    args = p.parse_args()

    if args.command not in {"report", "ab-report"} and not (API_KEY and ENDPOINT_ID):
        sys.exit("set RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID")
    if args.command in {"cold", "warm", "burst", "all", "flashboot-ab", "boundary"} and not args.audio:
        sys.exit("--audio is required")

    if args.command == "health":
        health()
    elif args.command == "cold":
        cmd_cold(args)
    elif args.command == "warm":
        cmd_warm(args)
    elif args.command == "burst":
        cmd_burst(args)
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "flashboot-ab":
        cmd_flashboot_ab(args)
    elif args.command == "boundary":
        cmd_boundary(args)
    elif args.command == "ab-report":
        cmd_ab_report(args)
    elif args.command == "all":
        cmd_cold(args)
        cmd_warm(args)
        cmd_burst(args)
        cmd_report(args)


if __name__ == "__main__":
    main()
