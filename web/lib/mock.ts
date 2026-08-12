import type { TranscriptionOutput } from "./types";

/**
 * A synthetic transcription so the whole app layer -- DP, renderer, UI -- can be
 * built and tuned without a GPU bill. Used when RUNPOD_* is unset, and flagged
 * as `source: "mock"` in the response so nobody mistakes it for real output.
 *
 * It is a 12-bar blues walking line in E at 96bpm, which exercises the parts of
 * the fretboard DP that matter: open strings, string crossings, and a run that
 * should stay in one hand position rather than sprinting up the neck.
 */

const BPM = 96;
const BEAT = 60 / BPM;

// Scale degrees over a 12-bar blues in E, one note per beat.
const ROOTS = [40, 40, 40, 40, 45, 45, 40, 40, 47, 45, 40, 47]; // E A B per bar
const WALK = [0, 4, 7, 9]; // root, third-ish, fifth, sixth -- a plain walking figure

export function mockTranscription(): TranscriptionOutput {
  const notes = [];
  let t = 0;
  for (let bar = 0; bar < ROOTS.length; bar++) {
    for (let beat = 0; beat < 4; beat++) {
      notes.push({
        onset: Number(t.toFixed(4)),
        duration: Number((BEAT * 0.85).toFixed(4)),
        midi: ROOTS[bar] + WALK[beat],
        confidence: Number((0.82 + 0.15 * Math.random()).toFixed(3)),
      });
      t += BEAT;
    }
  }

  return {
    notes,
    tempo_bpm_estimate: BPM,
    duration_s: Number(t.toFixed(2)),
    timings: { separation_s: 0, pitch_s: 0, total_s: 0 },
  };
}
