/**
 * Phase 4 -- evaluation harness. `npm run eval`
 *
 * Ground truth lives in eval/ground-truth/<song>.json, worker output in
 * eval/predictions/<song>.json. Both are matched by filename.
 *
 * Three metrics, deliberately separated so a regression tells you *which* stage
 * moved:
 *
 *   note F1          onset within +/-50ms AND correct pitch. The headline number.
 *   pitch accuracy   of notes matched on onset alone, how many had the right
 *                    pitch -- isolates the pitch tracker from the segmenter.
 *                    Reported alongside the octave-error rate, which is the
 *                    characteristic CREPE failure on bass and the thing the
 *                    median filter in segment.py exists to fix.
 *   position accuracy of correctly transcribed notes, how many the DP put on the
 *                    same string and fret a human chose. Pure Phase 3 signal --
 *                    it cannot be improved by touching the worker.
 *
 * `--sweep` re-runs position accuracy across a grid of DP weights, which is the
 * point of having ground truth at all.
 */

import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join, basename } from "node:path";

import { DEFAULT_WEIGHTS, assignFretboard, type FretboardWeights } from "../lib/fretboard";
import type { Note } from "../lib/types";

const ONSET_TOLERANCE_S = 0.05;
const GT_DIR = join(process.cwd(), "eval/ground-truth");
const PRED_DIR = join(process.cwd(), "eval/predictions");

interface GroundTruthNote {
  onset: number;
  duration?: number;
  midi: number;
  /**
   * Optional. Synthetic fixtures omit these on purpose: a generated note has no
   * "correct" fingering to compare against, so position accuracy is only
   * meaningful against a tab a human actually played.
   */
  string?: number;
  fret?: number;
}

interface GroundTruth {
  song: string;
  tempo_bpm?: number;
  notes: GroundTruthNote[];
}

interface Metrics {
  song: string;
  nGt: number;
  nPred: number;
  precision: number;
  recall: number;
  f1: number;
  pitchAccuracy: number;
  octaveErrorRate: number;
  positionAccuracy: number | null;
  positionMatched: number;
}

/**
 * One-to-one greedy match in onset order. Greedy is fine here because the
 * tolerance (50ms) is far smaller than any realistic gap between bass notes, so
 * the assignment is effectively unambiguous.
 */
function match(
  gt: GroundTruthNote[],
  pred: Note[],
  requirePitch: boolean
): Array<[number, number]> {
  const used = new Set<number>();
  const pairs: Array<[number, number]> = [];
  for (let i = 0; i < gt.length; i++) {
    let best = -1;
    let bestDelta = Infinity;
    for (let j = 0; j < pred.length; j++) {
      if (used.has(j)) continue;
      const delta = Math.abs(pred[j].onset - gt[i].onset);
      if (delta > ONSET_TOLERANCE_S) continue;
      if (requirePitch && pred[j].midi !== gt[i].midi) continue;
      if (delta < bestDelta) {
        bestDelta = delta;
        best = j;
      }
    }
    if (best >= 0) {
      used.add(best);
      pairs.push([i, best]);
    }
  }
  return pairs;
}

function evaluate(
  gt: GroundTruth,
  pred: Note[],
  weights: Partial<FretboardWeights> = {}
): Metrics {
  const strict = match(gt.notes, pred, true);
  const precision = pred.length ? strict.length / pred.length : 0;
  const recall = gt.notes.length ? strict.length / gt.notes.length : 0;
  const f1 = precision + recall ? (2 * precision * recall) / (precision + recall) : 0;

  const loose = match(gt.notes, pred, false);
  const correctPitch = loose.filter(([i, j]) => pred[j].midi === gt.notes[i].midi).length;
  const octaveErrors = loose.filter(
    ([i, j]) => Math.abs(pred[j].midi - gt.notes[i].midi) === 12
  ).length;

  // Position accuracy needs a human's fingering to compare against. Ground truth
  // without string/fret (any synthetic fixture) reports null rather than 0% --
  // "we did not measure this" and "we measured it and got nothing right" are very
  // different claims and must not look identical in the table.
  const hasPositions = gt.notes.some((n) => n.string !== undefined && n.fret !== undefined);
  const positioned = assignFretboard(pred, { weights });
  const byOnset = new Map(positioned.map((n) => [n.onset, n]));
  let posMatch = 0;
  let posComparable = 0;
  for (const [i, j] of strict) {
    const truth = gt.notes[i];
    if (truth.string === undefined || truth.fret === undefined) continue;
    posComparable++;
    const p = byOnset.get(pred[j].onset)?.position;
    if (p && p.string === truth.string && p.fret === truth.fret) posMatch++;
  }

  return {
    song: gt.song,
    nGt: gt.notes.length,
    nPred: pred.length,
    precision,
    recall,
    f1,
    pitchAccuracy: loose.length ? correctPitch / loose.length : 0,
    octaveErrorRate: loose.length ? octaveErrors / loose.length : 0,
    positionAccuracy: hasPositions && posComparable ? posMatch / posComparable : null,
    positionMatched: posComparable,
  };
}

function loadPairs(): Array<{ gt: GroundTruth; pred: Note[] }> {
  if (!existsSync(GT_DIR)) return [];
  const out = [];
  for (const file of readdirSync(GT_DIR).filter((f) => f.endsWith(".json")).sort()) {
    const predPath = join(PRED_DIR, basename(file));
    if (!existsSync(predPath)) {
      console.warn(`skipping ${file}: no prediction at eval/predictions/${basename(file)}`);
      continue;
    }
    const gt = JSON.parse(readFileSync(join(GT_DIR, file), "utf8")) as GroundTruth;
    const raw = JSON.parse(readFileSync(predPath, "utf8"));
    out.push({ gt, pred: (raw.notes ?? raw) as Note[] });
  }
  return out;
}

const pct = (x: number) => `${(100 * x).toFixed(1)}%`;
const f3 = (x: number) => x.toFixed(3);

function main() {
  const pairs = loadPairs();
  if (pairs.length === 0) {
    console.log(
      "No evaluable songs.\n" +
        "  1. put human tabs in eval/ground-truth/<song>.json\n" +
        "     { \"song\": \"...\", \"notes\": [{ \"onset\": 0.0, \"midi\": 40, \"string\": 0, \"fret\": 0 }] }\n" +
        "  2. put worker output in eval/predictions/<song>.json\n" +
        "  3. npm run eval\n\n" +
        "`npx tsx scripts/make-fixtures.ts` writes a synthetic pair to try it out."
    );
    return;
  }

  const results = pairs.map(({ gt, pred }) => evaluate(gt, pred));

  console.log(`\nonset tolerance ${ONSET_TOLERANCE_S * 1000}ms\n`);
  console.log("| song | gt | pred | P | R | F1 | pitch acc | octave err | position acc |");
  console.log("|---|---|---|---|---|---|---|---|---|");
  for (const r of results) {
    console.log(
      `| ${r.song} | ${r.nGt} | ${r.nPred} | ${f3(r.precision)} | ${f3(r.recall)} | ` +
        `${f3(r.f1)} | ${pct(r.pitchAccuracy)} | ${pct(r.octaveErrorRate)} | ${r.positionAccuracy === null ? "n/a" : pct(r.positionAccuracy)} |`
    );
  }

  const mean = (f: (r: Metrics) => number) =>
    results.reduce((a, r) => a + f(r), 0) / results.length;
  console.log(
    `| **mean** | | | ${f3(mean((r) => r.precision))} | ${f3(mean((r) => r.recall))} | ` +
      `${f3(mean((r) => r.f1))} | ${pct(mean((r) => r.pitchAccuracy))} | ` +
      `${pct(mean((r) => r.octaveErrorRate))} | ${(() => { const v = results.filter((r) => r.positionAccuracy !== null); return v.length ? pct(v.reduce((a, r) => a + (r.positionAccuracy as number), 0) / v.length) : "n/a"; })()} |`
  );

  if (process.argv.includes("--sweep")) {
    console.log("\n## DP weight sweep (position accuracy)\n");
    console.log("| fretHeight | stringChange | openBonus | position acc |");
    console.log("|---|---|---|---|");
    let best = { acc: -1, label: "" };
    for (const fretHeight of [0, 0.1, 0.2, 0.35, 0.6, 1.0]) {
      for (const stringChange of [0.5, 1.0, 1.6, 2.5, 4.0]) {
        for (const openBonus of [0, -2, -4]) {
          const w = { fretHeight, stringChange, openBonus };
          const acc =
            pairs.reduce((a, { gt, pred }) => a + (evaluate(gt, pred, w).positionAccuracy ?? 0), 0) /
            pairs.length;
          const label = `| ${fretHeight} | ${stringChange} | ${openBonus} | ${pct(acc)} |`;
          console.log(label);
          if (acc > best.acc) best = { acc, label };
        }
      }
    }
    console.log(
      `\nbest: ${best.label}\ndefaults: fretHeight ${DEFAULT_WEIGHTS.fretHeight}, ` +
        `stringChange ${DEFAULT_WEIGHTS.stringChange}, openBonus ${DEFAULT_WEIGHTS.openBonus}`
    );
  } else {
    console.log("\nrun with --sweep to grid-search the DP weights against this ground truth");
  }
}

main();
