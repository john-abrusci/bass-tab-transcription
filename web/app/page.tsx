"use client";

import { useRef, useState } from "react";

import TabViewer from "@/components/TabViewer";
import type { TranscribeResponse } from "@/lib/types";

export default function Home() {
  const [file, setFile] = useState<File | null>(null);
  const [maxDuration, setMaxDuration] = useState(120);
  const [confidence, setConfidence] = useState(0.5);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<TranscribeResponse | null>(null);
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  async function run() {
    if (!file) return;
    setBusy(true);
    setError(null);
    setResult(null);

    const form = new FormData();
    form.set("file", file);
    form.set("max_duration_s", String(maxDuration));
    form.set("confidence_threshold", String(confidence));

    try {
      const res = await fetch("/api/transcribe", { method: "POST", body: form });
      const json = await res.json();
      if (!res.ok) throw new Error(json.error ?? `request failed (${res.status})`);
      setResult(json as TranscribeResponse);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <header>
        <h1>Bass Tab Transcription</h1>
        <p>
          htdemucs isolates the bass, torchcrepe tracks pitch — both in one Runpod
          Serverless worker. Fretboard assignment and rendering happen here.
        </p>
      </header>

      <section>
        <h2>Audio</h2>
        <div
          className={`drop${over ? " over" : ""}`}
          onClick={() => input.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setOver(true);
          }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setOver(false);
            const f = e.dataTransfer.files?.[0];
            if (f) setFile(f);
          }}
        >
          <strong>{file ? file.name : "Drop an audio file, or click to choose"}</strong>
          <span>
            {file
              ? `${(file.size / 1e6).toFixed(1)} MB — base64 adds a third to that on the wire`
              : "mp3, wav, flac, m4a — anything ffmpeg can decode. 24 MB max."}
          </span>
          <input
            ref={input}
            type="file"
            accept="audio/*"
            hidden
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </div>

        <div className="controls">
          <div>
            <label htmlFor="dur">Transcribe first N seconds</label>
            <input
              id="dur"
              type="number"
              min={5}
              max={600}
              value={maxDuration}
              onChange={(e) => setMaxDuration(Number(e.target.value))}
            />
          </div>
          <div>
            <label htmlFor="conf">Confidence gate · {confidence.toFixed(2)}</label>
            <input
              id="conf"
              type="range"
              min={0.1}
              max={0.9}
              step={0.05}
              value={confidence}
              onChange={(e) => setConfidence(Number(e.target.value))}
            />
          </div>
        </div>

        <div className="row" style={{ marginTop: 20 }}>
          <button onClick={run} disabled={!file || busy}>
            {busy ? (
              <>
                <span className="spinner" /> Transcribing…
              </>
            ) : (
              "Transcribe"
            )}
          </button>
          {busy && (
            <span className="muted">
              A cold worker pays container pull plus model load before it starts. First
              run after an idle period is the slow one.
            </span>
          )}
        </div>

        {error && (
          <div className="panel error" style={{ marginTop: 20 }}>
            {error}
          </div>
        )}
      </section>

      {result && <TabViewer result={result} />}
    </main>
  );
}
