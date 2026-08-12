/**
 * Writes a synthetic ground-truth/prediction pair so the eval harness can be
 * exercised before you have transcribed anything real.
 *
 * The "prediction" is the ground truth with the failure modes this pipeline
 * actually has injected at plausible rates: dropped quiet notes, octave errors
 * from CREPE, onset jitter from the segmenter's frame grid, and a few spurious
 * short notes from bleed in the separated stem. That means the harness reports
 * numbers in a believable range rather than a perfect 1.0, and you can tell at
 * a glance whether a metric is wired up correctly.
 *
 * Replace both files with real data -- a human tab you trust, and real worker
 * output -- before drawing any conclusions from the numbers.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { STANDARD_TUNING } from "../lib/fretboard";

const BPM = 96;
const BEAT = 60 / BPM;
const ROOTS = [40, 40, 40, 40, 45, 45, 40, 40, 47, 45, 40, 47];
const WALK = [0, 4, 7, 9];

/** How a player in open position would finger it: lowest string reachable under fret 6. */
function humanPosition(midi: number) {
  for (let s = 0; s < STANDARD_TUNING.length; s++) {
    const fret = midi - STANDARD_TUNING[s];
    if (fret >= 0 && fret <= 5) return { string: s, fret };
  }
  for (let s = STANDARD_TUNING.length - 1; s >= 0; s--) {
    const fret = midi - STANDARD_TUNING[s];
    if (fret >= 0 && fret <= 22) return { string: s, fret };
  }
  throw new Error(`unplayable: ${midi}`);
}

// Fixed seed: the fixture must not change between runs, or eval numbers drift
// for reasons that have nothing to do with the pipeline.
let seed = 20240611;
function rand() {
  seed = (seed * 1103515245 + 12345) & 0x7fffffff;
  return seed / 0x7fffffff;
}

const gtNotes = [];
let t = 0;
for (const root of ROOTS) {
  for (const step of WALK) {
    const midi = root + step;
    gtNotes.push({
      onset: Number(t.toFixed(4)),
      duration: Number((BEAT * 0.85).toFixed(4)),
      midi,
      ...humanPosition(midi),
    });
    t += BEAT;
  }
}

const predNotes = [];
for (const n of gtNotes) {
  if (rand() < 0.08) continue; // missed note
  let midi = n.midi;
  if (rand() < 0.06) midi += 12; // CREPE octave error
  predNotes.push({
    onset: Number((n.onset + (rand() - 0.5) * 0.05).toFixed(4)), // frame-grid jitter
    duration: Number((n.duration * (0.85 + rand() * 0.3)).toFixed(4)),
    midi,
    confidence: Number((0.6 + rand() * 0.39).toFixed(3)),
  });
}
for (let i = 0; i < 3; i++) {
  // Spurious short notes: bleed from the other stems that survived the gate.
  predNotes.push({
    onset: Number((rand() * t).toFixed(4)),
    duration: 0.08,
    midi: 40 + Math.floor(rand() * 12),
    confidence: 0.55,
  });
}
predNotes.sort((a, b) => a.onset - b.onset);

const gtDir = join(process.cwd(), "eval/ground-truth");
const predDir = join(process.cwd(), "eval/predictions");
mkdirSync(gtDir, { recursive: true });
mkdirSync(predDir, { recursive: true });

writeFileSync(
  join(gtDir, "fixture-blues-in-e.json"),
  JSON.stringify(
    {
      song: "fixture-blues-in-e (SYNTHETIC)",
      tempo_bpm: BPM,
      notes: gtNotes,
    },
    null,
    2
  )
);
writeFileSync(
  join(predDir, "fixture-blues-in-e.json"),
  JSON.stringify(
    { notes: predNotes, tempo_bpm_estimate: BPM, duration_s: Number(t.toFixed(2)) },
    null,
    2
  )
);

console.log(
  `wrote fixture: ${gtNotes.length} ground-truth notes, ${predNotes.length} predicted`
);
