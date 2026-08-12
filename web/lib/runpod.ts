import type { TranscriptionOutput } from "./types";

const RUNSYNC_TIMEOUT_MS = 10 * 60 * 1000;

export interface TranscribeOptions {
  maxDurationS?: number;
  confidenceThreshold?: number;
  returnStem?: boolean;
}

export function runpodConfigured(): boolean {
  return !!(process.env.RUNPOD_API_KEY && process.env.RUNPOD_ENDPOINT_ID);
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

  try {
    const res = await fetch(`https://api.runpod.ai/v2/${endpoint}/runsync`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${key}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        input: {
          audio_b64: audio.toString("base64"),
          max_duration_s: opts.maxDurationS ?? 300,
          confidence_threshold: opts.confidenceThreshold ?? 0.5,
          return_stem: opts.returnStem ?? false,
        },
      }),
      signal: controller.signal,
    });

    if (!res.ok) {
      throw new Error(`Runpod returned ${res.status}: ${(await res.text()).slice(0, 300)}`);
    }

    const json = await res.json();
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
