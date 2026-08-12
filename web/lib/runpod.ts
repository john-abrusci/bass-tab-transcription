import type { TranscriptionOutput } from "./types";

const RUNSYNC_TIMEOUT_MS = 10 * 60 * 1000;
const MAX_UPLOAD_ATTEMPTS = 4;

export interface TranscribeOptions {
  maxDurationS?: number;
  confidenceThreshold?: number;
  returnStem?: boolean;
}

export function runpodConfigured(): boolean {
  return !!(process.env.RUNPOD_API_KEY && process.env.RUNPOD_ENDPOINT_ID);
}

const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"]);
const POLL_INTERVAL_MS = 2000;

/**
 * Poll /status until the job is done.
 *
 * /runsync waits a bounded window and then returns `{status: "IN_QUEUE", id}`
 * rather than blocking to completion. A cold start on this endpoint measured
 * ~220s, far past that window, so without this every cold request surfaces to
 * the user as a failure.
 */
async function awaitJob(
  first: any,
  endpoint: string,
  key: string,
  signal: AbortSignal
): Promise<any> {
  let job = first;
  while (job?.status && !TERMINAL.has(job.status)) {
    if (!job.id) return job;
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    if (signal.aborted) throw new Error("timed out waiting for Runpod job");
    const res = await fetch(`https://api.runpod.ai/v2/${endpoint}/status/${job.id}`, {
      headers: { Authorization: `Bearer ${key}` },
      signal,
    });
    if (!res.ok) throw new Error(`status poll returned ${res.status}`);
    job = await res.json();
  }
  return job;
}

/**
 * Call the serverless endpoint synchronously.
 *
 * /runsync rather than /run + poll: a 4-minute track is well inside the
 * execution timeout, and one blocking call keeps the app layer simple. If
 * tracks get long enough to bump the timeout, switch to /run and poll /status
 * -- the response shape of the `output` object is identical either way.
 */
export async function transcribe(
  audio: Buffer,
  opts: TranscribeOptions = {}
): Promise<TranscriptionOutput> {
  const endpoint = process.env.RUNPOD_ENDPOINT_ID;
  const key = process.env.RUNPOD_API_KEY;
  if (!endpoint || !key) throw new Error("RUNPOD_ENDPOINT_ID / RUNPOD_API_KEY not set");

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), RUNSYNC_TIMEOUT_MS);

  const body = JSON.stringify({
    input: {
      audio_b64: audio.toString("base64"),
      max_duration_s: opts.maxDurationS ?? 300,
      confidence_threshold: opts.confidenceThreshold ?? 0.5,
      return_stem: opts.returnStem ?? false,
    },
  });

  try {
    // Large base64 uploads to this endpoint fail intermittently at the transport
    // layer -- broken pipe, occasionally a corrupted TLS record. Measured at ~80%
    // first-attempt failure for a 2.2MB payload, so a single attempt is not a
    // usable product. Retry connection-level failures only; an HTTP status means
    // the request arrived and retrying would just repeat it.
    let res: Response | undefined;
    let lastError: unknown;
    for (let attempt = 1; attempt <= MAX_UPLOAD_ATTEMPTS; attempt++) {
      try {
        res = await fetch(`https://api.runpod.ai/v2/${endpoint}/runsync`, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${key}`,
            "Content-Type": "application/json",
          },
          body,
          signal: controller.signal,
        });
        break;
      } catch (err) {
        if (controller.signal.aborted) throw err;
        lastError = err;
        if (attempt < MAX_UPLOAD_ATTEMPTS) {
          await new Promise((r) => setTimeout(r, 1000 * attempt));
        }
      }
    }
    if (!res) {
      throw new Error(
        `upload failed after ${MAX_UPLOAD_ATTEMPTS} attempts: ${
          lastError instanceof Error ? lastError.message : String(lastError)
        }`
      );
    }

    if (!res.ok) {
      throw new Error(`Runpod returned ${res.status}: ${(await res.text()).slice(0, 300)}`);
    }

    const json = await awaitJob(await res.json(), endpoint, key, controller.signal);
    if (json.status && json.status !== "COMPLETED") {
      throw new Error(`job ${json.status}: ${JSON.stringify(json.error ?? json).slice(0, 300)}`);
    }
    const output = json.output;
    if (!output) throw new Error("Runpod response had no output");
    if (output.error) throw new Error(`worker error: ${output.error}`);
    return output as TranscriptionOutput;
  } finally {
    clearTimeout(timer);
  }
}
