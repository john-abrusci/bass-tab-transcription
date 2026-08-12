/**
 * Phase 3 -- fretboard assignment.
 *
 * Every MIDI pitch a bass can play is available at several string/fret pairs.
 * Picking the one a human would actually play is a shortest-path problem, not a
 * lookup: the right choice for a note depends entirely on where the hand
 * already is. So: DP over note events, state = (string, fret), cost = how
 * awkward that transition is.
 *
 * No ML here at all. The weights below are taste, and they are meant to be
 * tuned against tabs you already know are right (see scripts/eval.ts).
 */

import type { Note, PositionedNote, Position } from "./types";

/** Open-string pitches, low to high. E1 A1 D2 G2. */
export const STANDARD_TUNING = [28, 33, 38, 43];
export const STRING_LABELS = ["E", "A", "D", "G"];

/** 5-string, low B. Handy for anything that dips below E1. */
export const FIVE_STRING_TUNING = [23, 28, 33, 38, 43];
export const FIVE_STRING_LABELS = ["B", "E", "A", "D", "G"];

export interface FretboardWeights {
  /** Cost per fret of hand movement between consecutive notes. */
  fretMove: number;
  /** Cost per string crossed. */
  stringChange: number;
  /** Credit for an open string -- free, rings out, no hand cost. */
  openBonus: number;
  /**
   * Mild absolute cost per fret. Without this the transition costs alone are
   * degenerate -- E12, A7 and D2 are the same pitch and moving between those
   * regions is nearly free, so a repeated figure drifts to a different part of
   * the neck each time it comes round. This is the anchor that makes the whole
   * line pick one place and stay there.
   */
  fretHeight: number;
  /** Frets above this start costing extra (cramped, and hard to read). */
  highFretStart: number;
  highFret: number;
  /** A hand covers about this many frets without shifting. */
  handSpan: number;
  /** Cost per fret of stretch beyond handSpan -- a real position shift. */
  spanPenalty: number;
  /**
   * Movement gets cheap when there is time to move. A gap of this many seconds
   * halves the cost of a shift.
   */
  freeMoveGapS: number;
}

export const DEFAULT_WEIGHTS: FretboardWeights = {
  fretMove: 1.0,
  stringChange: 1.6,
  openBonus: -2.0,
  fretHeight: 0.35,
  highFretStart: 12,
  highFret: 1.2,
  handSpan: 4,
  spanPenalty: 2.5,
  freeMoveGapS: 0.5,
};

export interface FretboardOptions {
  tuning?: number[];
  maxFret?: number;
  weights?: Partial<FretboardWeights>;
}

/** Every string/fret pair on this instrument that sounds `midi`. */
export function positionsFor(
  midi: number,
  tuning: number[] = STANDARD_TUNING,
  maxFret = 22
): Position[] {
  const out: Position[] = [];
  for (let s = 0; s < tuning.length; s++) {
    const fret = midi - tuning[s];
    if (fret >= 0 && fret <= maxFret) out.push({ string: s, fret });
  }
  return out;
}

/** Cost of a position considered on its own, ignoring where the hand was. */
function positionCost(pos: Position, w: FretboardWeights): number {
  let c = 0;
  if (pos.fret === 0) {
    c += w.openBonus;
  } else {
    c += pos.fret * w.fretHeight;
    c += Math.max(0, pos.fret - w.highFretStart) * w.highFret;
  }
  return c;
}

/** Cost of moving from `prev` to `cur`, given the time available to do it. */
function transitionCost(
  prev: Position,
  cur: Position,
  gapS: number,
  w: FretboardWeights
): number {
  // An open string does not pin the hand anywhere, so treat it as free movement
  // rather than as a position at fret 0 -- otherwise every open E reads as a
  // huge shift down the neck and back.
  const anchored = prev.fret !== 0 && cur.fret !== 0;
  const fretDelta = anchored ? Math.abs(cur.fret - prev.fret) : 0;

  // More time between notes, cheaper the shift. A run of sixteenths should
  // stay in position; a whole bar of rest means you can go anywhere.
  const mobility = 1 / (1 + Math.max(0, gapS) / w.freeMoveGapS);

  let c = fretDelta * w.fretMove * mobility;
  c += Math.max(0, fretDelta - w.handSpan) * w.spanPenalty * mobility;
  c += Math.abs(cur.string - prev.string) * w.stringChange * mobility;
  return c;
}

/**
 * Assign a string/fret to every note by minimising total path cost.
 *
 * Notes with no playable position (below the lowest open string, or above the
 * top fret) come back with `position: null` and are skipped by the path, so
 * one out-of-range note does not reset the hand.
 */
export function assignFretboard(
  notes: Note[],
  options: FretboardOptions = {}
): PositionedNote[] {
  const tuning = options.tuning ?? STANDARD_TUNING;
  const maxFret = options.maxFret ?? 22;
  const w = { ...DEFAULT_WEIGHTS, ...(options.weights ?? {}) };

  const sorted = [...notes].sort((a, b) => a.onset - b.onset);
  if (sorted.length === 0) return [];

  const candidates = sorted.map((n) => positionsFor(n.midi, tuning, maxFret));

  // dp[j] = cheapest total cost of a path ending at candidates[i][j].
  let dp: number[] = [];
  let prevIndex: number[][] = []; // prevIndex[i][j] = chosen j at the previous *playable* note
  let lastPlayable = -1; // index into `sorted`

  for (let i = 0; i < sorted.length; i++) {
    const cands = candidates[i];
    prevIndex.push([]);
    if (cands.length === 0) continue; // unplayable: leave the hand where it was

    const cost = new Array(cands.length).fill(Infinity);
    for (let j = 0; j < cands.length; j++) {
      const base = positionCost(cands[j], w);
      if (lastPlayable < 0) {
        cost[j] = base;
        prevIndex[i][j] = -1;
        continue;
      }
      const prevCands = candidates[lastPlayable];
      const gapS = Math.max(
        0,
        sorted[i].onset - (sorted[lastPlayable].onset + sorted[lastPlayable].duration)
      );
      let best = Infinity;
      let bestK = -1;
      for (let k = 0; k < prevCands.length; k++) {
        const total = dp[k] + transitionCost(prevCands[k], cands[j], gapS, w);
        if (total < best) {
          best = total;
          bestK = k;
        }
      }
      cost[j] = best + base;
      prevIndex[i][j] = bestK;
    }
    dp = cost;
    lastPlayable = i;
  }

  const result: PositionedNote[] = sorted.map((n) => ({ ...n, position: null }));
  if (lastPlayable < 0) return result;

  // Backtrack from the cheapest endpoint.
  let j = dp.indexOf(Math.min(...dp));
  let i = lastPlayable;
  while (i >= 0 && j >= 0) {
    result[i].position = candidates[i][j];
    const k = prevIndex[i][j];
    // Step back to the previous note that had any candidates at all.
    let p = i - 1;
    while (p >= 0 && candidates[p].length === 0) p--;
    i = p;
    j = k;
  }
  return result;
}

/** Total path cost of an assignment -- used by the eval harness to compare weights. */
export function pathCost(
  notes: PositionedNote[],
  options: FretboardOptions = {}
): number {
  const w = { ...DEFAULT_WEIGHTS, ...(options.weights ?? {}) };
  const played = notes.filter((n) => n.position);
  let total = 0;
  for (let i = 0; i < played.length; i++) {
    total += positionCost(played[i].position!, w);
    if (i > 0) {
      const gapS = Math.max(
        0,
        played[i].onset - (played[i - 1].onset + played[i - 1].duration)
      );
      total += transitionCost(played[i - 1].position!, played[i].position!, gapS, w);
    }
  }
  return total;
}
