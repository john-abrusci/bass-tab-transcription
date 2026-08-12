"use client";

import { useMemo, useState } from "react";

import {
  DEFAULT_WEIGHTS,
  FIVE_STRING_LABELS,
  FIVE_STRING_TUNING,
  STANDARD_TUNING,
  STRING_LABELS,
  assignFretboard,
} from "@/lib/fretboard";
import { formatTime, renderTab, verifyPositions } from "@/lib/tab";
import { midiToName, type TranscribeResponse } from "@/lib/types";

interface Props {
  result: TranscribeResponse;
}

export default function TabViewer({ result }: Props) {
  // Phase 3 weights are exposed live: re-running the DP is microseconds, so
  // tuning them should not cost another GPU job.
  const [stringChange, setStringChange] = useState(DEFAULT_WEIGHTS.stringChange);
  const [openBonus, setOpenBonus] = useState(DEFAULT_WEIGHTS.openBonus);
  const [maxFret, setMaxFret] = useState(17);
  const [fiveString, setFiveString] = useState(false);
  const [gridded, setGridded] = useState(true);

  const tuning = fiveString ? FIVE_STRING_TUNING : STANDARD_TUNING;
  const labels = fiveString ? FIVE_STRING_LABELS : STRING_LABELS;

  const positioned = useMemo(
    () =>
      assignFretboard(result.notes, {
        tuning,
        maxFret,
        weights: { stringChange, openBonus },
      }),
    [result.notes, tuning, maxFret, stringChange, openBonus]
  );

  const tab = useMemo(
    () =>
      renderTab(positioned, {
        tempo: gridded ? result.tempo_bpm_estimate : null,
        labels,
      }),
    [positioned, gridded, result.tempo_bpm_estimate, labels]
  );

  const unplayable = positioned.filter((n) => !n.position).length;
  const mismatches = useMemo(() => verifyPositions(positioned, tuning), [positioned, tuning]);
  const t = result.timings;

  function download(name: string, text: string, type = "text/plain") {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <>
      <section>
        <h2>Run</h2>
        <div className="stats">
          <Stat k="Notes" v={String(result.notes.length)} />
          <Stat
            k="Tempo"
            v={result.tempo_bpm_estimate ? `${result.tempo_bpm_estimate} bpm` : "—"}
          />
          <Stat k="Audio" v={formatTime(result.duration_s)} />
          <Stat k="Separation" v={`${t.separation_s}s`} />
          <Stat k="Pitch" v={`${t.pitch_s}s`} />
          <Stat k="Round trip" v={`${result.round_trip_s}s`} />
        </div>
        <p className="muted" style={{ marginTop: 10 }}>
          {result.source === "mock" ? (
            <>
              <span className="badge">mock</span> No Runpod endpoint configured — this is a
              synthetic bassline so the tab layer stays usable offline.
            </>
          ) : (
            <>
              Worker time {t.total_s}s
              {t.model_load_s ? `, model load ${t.model_load_s}s` : ""}
              {t.cold ? " (cold worker)" : ""}. The gap between worker time and round
              trip is payload transfer plus queue wait:{" "}
              {(result.round_trip_s - t.total_s).toFixed(1)}s.
            </>
          )}
        </p>
      </section>

      <section>
        <h2>Fretboard assignment</h2>
        <div className="panel">
          <div className="controls">
            <div>
              <label htmlFor="sc">String-change penalty · {stringChange.toFixed(1)}</label>
              <input
                id="sc"
                type="range"
                min={0}
                max={5}
                step={0.1}
                value={stringChange}
                onChange={(e) => setStringChange(Number(e.target.value))}
              />
            </div>
            <div>
              <label htmlFor="ob">Open-string bonus · {openBonus.toFixed(1)}</label>
              <input
                id="ob"
                type="range"
                min={-6}
                max={0}
                step={0.1}
                value={openBonus}
                onChange={(e) => setOpenBonus(Number(e.target.value))}
              />
            </div>
            <div>
              <label htmlFor="mf">Highest fret · {maxFret}</label>
              <input
                id="mf"
                type="range"
                min={5}
                max={24}
                step={1}
                value={maxFret}
                onChange={(e) => setMaxFret(Number(e.target.value))}
              />
            </div>
            <div>
              <label htmlFor="inst">Instrument</label>
              <select
                id="inst"
                value={fiveString ? "5" : "4"}
                onChange={(e) => setFiveString(e.target.value === "5")}
              >
                <option value="4">4-string (EADG)</option>
                <option value="5">5-string (BEADG)</option>
              </select>
            </div>
          </div>
          {(unplayable > 0 || mismatches.length > 0) && (
            <p className="muted" style={{ marginTop: 14 }}>
              {unplayable > 0 &&
                `${unplayable} note${unplayable === 1 ? "" : "s"} outside the neck at these settings. `}
              {mismatches.length > 0 && `${mismatches.length} pitch mismatches (bug).`}
            </p>
          )}
        </div>
      </section>

      <section>
        <h2>Tab</h2>
        <div className="panel">
          <div className="row" style={{ marginBottom: 14 }}>
            <button
              className="secondary"
              onClick={() => setGridded((g) => !g)}
              disabled={!result.tempo_bpm_estimate}
            >
              {gridded ? "Sequential layout" : "Beat-grid layout"}
            </button>
            <button className="secondary" onClick={() => download("bass-tab.txt", tab)}>
              Download .txt
            </button>
            <button
              className="secondary"
              onClick={() =>
                download(
                  "notes.json",
                  JSON.stringify({ ...result, positioned }, null, 2),
                  "application/json"
                )
              }
            >
              Download .json
            </button>
            {!result.tempo_bpm_estimate && (
              <span className="muted">No tempo estimate — sequential layout only.</span>
            )}
          </div>
          <pre className="tab">{tab}</pre>
        </div>
      </section>

      <section>
        <details>
          <summary>Note events ({positioned.length})</summary>
          <div className="panel" style={{ marginTop: 12, maxHeight: 420, overflowY: "auto" }}>
            <table>
              <thead>
                <tr>
                  <th>Onset</th>
                  <th>Dur</th>
                  <th>Pitch</th>
                  <th>MIDI</th>
                  <th>String/fret</th>
                  <th>Conf</th>
                </tr>
              </thead>
              <tbody>
                {positioned.map((n, i) => (
                  <tr key={i}>
                    <td className="mono">{n.onset.toFixed(3)}</td>
                    <td className="mono">{n.duration.toFixed(3)}</td>
                    <td>{midiToName(n.midi)}</td>
                    <td className="mono">{n.midi}</td>
                    <td className="mono">
                      {n.position ? `${labels[n.position.string]}${n.position.fret}` : "—"}
                    </td>
                    <td className="mono">{n.confidence.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      </section>
    </>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return (
    <div className="stat">
      <div className="k">{k}</div>
      <div className="v">{v}</div>
    </div>
  );
}
