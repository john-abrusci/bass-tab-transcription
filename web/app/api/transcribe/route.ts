import { NextResponse } from "next/server";

import { mockTranscription } from "@/lib/mock";
import { runpodConfigured, transcribe } from "@/lib/runpod";
import type { TranscribeResponse } from "@/lib/types";

export const runtime = "nodejs";
// Separation on a mid-tier GPU runs ~15s for a 4-minute track, plus cold start.
export const maxDuration = 300;

const MAX_UPLOAD_BYTES = 24 * 1024 * 1024;

export async function POST(req: Request) {
  const started = Date.now();

  let form: FormData;
  try {
    form = await req.formData();
  } catch {
    return NextResponse.json({ error: "expected multipart/form-data" }, { status: 400 });
  }

  const file = form.get("file");
  const maxDurationS = Number(form.get("max_duration_s") ?? 300);
  const confidenceThreshold = Number(form.get("confidence_threshold") ?? 0.5);

  if (!(file instanceof File)) {
    return NextResponse.json({ error: "no file uploaded" }, { status: 400 });
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return NextResponse.json(
      { error: `file is ${(file.size / 1e6).toFixed(1)}MB; limit is ${MAX_UPLOAD_BYTES / 1e6}MB` },
      { status: 413 }
    );
  }

  // No endpoint configured: serve the mock so the app layer is still usable.
  if (!runpodConfigured()) {
    const output = mockTranscription();
    const body: TranscribeResponse = {
      ...output,
      round_trip_s: Number(((Date.now() - started) / 1000).toFixed(2)),
      source: "mock",
    };
    return NextResponse.json(body);
  }

  try {
    const buf = Buffer.from(await file.arrayBuffer());
    const output = await transcribe(buf, { maxDurationS, confidenceThreshold });
    const body: TranscribeResponse = {
      ...output,
      round_trip_s: Number(((Date.now() - started) / 1000).toFixed(2)),
      source: "runpod",
    };
    return NextResponse.json(body);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
