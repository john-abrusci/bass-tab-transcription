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


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["health", "cold", "warm", "burst", "all", "report"])
    p.add_argument("--audio", help="path to a test track")
    p.add_argument("--n", type=int, default=5)
    p.add_argument("--max-s", dest="max_s", type=int, default=300)
    p.add_argument("--conf", type=float, default=0.5)
    p.add_argument("--label", default="unlabelled", help="e.g. 'RTX 4090, weights baked in'")
    args = p.parse_args()

    if args.command != "report" and not (API_KEY and ENDPOINT_ID):
        sys.exit("set RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID")
    if args.command in {"cold", "warm", "burst", "all"} and not args.audio:
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
    elif args.command == "all":
        cmd_cold(args)
        cmd_warm(args)
        cmd_burst(args)
        cmd_report(args)


if __name__ == "__main__":
    main()
