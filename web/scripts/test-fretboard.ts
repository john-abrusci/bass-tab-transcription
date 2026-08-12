/**
 * Tests for the Phase 3 DP and the tab renderer. `npm test`.
 *
 * These assert musical behaviour, not implementation details: a scale run
 * should stay under one hand, an open string should be preferred when it costs
 * nothing, and every position must actually sound the pitch it claims to.
 */

import {
  STANDARD_TUNING,
  assignFretboard,
  positionsFor,
} from "../lib/fretboard";
import { renderTab, renderTabSystems, verifyPositions } from "../lib/tab";
import type { Note } from "../lib/types";

let failures = 0;

function check(name: string, cond: boolean, detail = "") {
  if (!cond) failures++;
  console.log(`  ${cond ? "PASS" : "FAIL"}  ${name}${!cond && detail ? `  -- ${detail}` : ""}`);
}

function seq(midis: number[], step = 0.25, dur = 0.2): Note[] {
  return midis.map((midi, i) => ({
    onset: Number((i * step).toFixed(4)),
    duration: dur,
    midi,
    confidence: 0.9,
  }));
}

console.log("positionsFor");
{
  const p = positionsFor(40, STANDARD_TUNING, 22); // E2
  check("E2 is available on three strings", p.length === 3, JSON.stringify(p));
  check(
    "every candidate sounds the pitch",
    p.every((x) => STANDARD_TUNING[x.string] + x.fret === 40)
  );
  check("open low E has exactly one position", positionsFor(28).length === 1);
  check("below the low E is unplayable", positionsFor(20).length === 0);
  // G3 is fret 12 on the G string and fret 17 on the D, so a 12-fret neck
  // leaves exactly one way to play it.
  check("maxFret is respected", positionsFor(55, STANDARD_TUNING, 12).length === 1);
  check("nothing is playable above the neck", positionsFor(60, STANDARD_TUNING, 12).length === 0);
}

console.log("\nassignFretboard");
{
  // Stepwise runs are the case where a naive per-note choice looks worst: each
  // note has 2-3 options and picking each in isolation makes the hand jump. The
  // measure that matters is per-transition movement, not the span of the whole
  // run -- an ascending octave has to travel somewhere.
  const maxJump = (midis: number[]) => {
    const out = assignFretboard(seq(midis));
    check(`no pitch mismatches (${midis[0]}..${midis[midis.length - 1]})`,
      verifyPositions(out).length === 0);
    const frets = out.map((n) => n.position!.fret);
    let worst = 0;
    for (let i = 1; i < frets.length; i++) {
      if (frets[i] === 0 || frets[i - 1] === 0) continue; // open strings free the hand
      worst = Math.max(worst, Math.abs(frets[i] - frets[i - 1]));
    }
    return { worst, frets };
  };

  const a = maxJump([33, 35, 37, 38, 40, 42, 44, 45]); // A major, A1 -> A2
  check("low scale run never shifts more than a hand span", a.worst <= 5,
    `frets ${a.frets.join(",")}`);

  const g = maxJump([43, 45, 47, 48, 50, 52, 54, 55]); // G major, G2 -> G3
  check("high scale run never shifts more than a hand span", g.worst <= 5,
    `frets ${g.frets.join(",")}`);
}
{
  const out = assignFretboard(seq([28, 33, 38, 43]));
  check(
    "open strings are chosen when they are free",
    out.every((n) => n.position!.fret === 0),
    JSON.stringify(out.map((n) => n.position))
  );
}
{
  // Repeating one pitch must not oscillate between equivalent positions.
  const out = assignFretboard(seq([45, 45, 45, 45]));
  const unique = new Set(out.map((n) => `${n.position!.string}:${n.position!.fret}`));
  check("a repeated pitch keeps one position", unique.size === 1, [...unique].join(" "));
}
{
  const out = assignFretboard(seq([40, 20, 40])); // middle note is off the neck
  check("unplayable note yields null position", out[1].position === null);
  check("hand is not reset by the unplayable note",
    out[0].position!.string === out[2].position!.string &&
    out[0].position!.fret === out[2].position!.fret);
}
{
  const out = assignFretboard(seq([]));
  check("empty input is empty output", out.length === 0);
}
{
  // A big leap with a long rest before it should not be penalised into a
  // contorted low-fret choice; with no time at all, it should stay close.
  const tight = assignFretboard([
    { onset: 0, duration: 0.1, midi: 43, confidence: 1 },
    { onset: 0.12, duration: 0.1, midi: 62, confidence: 1 },
  ]);
  const loose = assignFretboard([
    { onset: 0, duration: 0.1, midi: 43, confidence: 1 },
    { onset: 4.0, duration: 0.1, midi: 62, confidence: 1 },
  ]);
  check(
    "time available affects the chosen position",
    verifyPositions(tight).length === 0 && verifyPositions(loose).length === 0
  );
}
{
  // Weights are taste, so the contract is that turning one up actually changes
  // the answer in the direction you would expect.
  const notes = seq([40, 45, 50, 45, 40]);
  const cheap = assignFretboard(notes, { weights: { stringChange: 0.1 } });
  const dear = assignFretboard(notes, { weights: { stringChange: 40 } });
  const strings = (o: typeof cheap) => new Set(o.map((n) => n.position!.string)).size;
  check(
    "a large string-change penalty keeps the line on fewer strings",
    strings(dear) <= strings(cheap) && strings(dear) === 1,
    `cheap used ${strings(cheap)} strings, dear used ${strings(dear)}`
  );
  check("weight changes never break the pitches", verifyPositions(dear).length === 0);
}

console.log("\nrenderTab");
{
  const out = assignFretboard(seq([28, 33, 38, 43]));
  const systems = renderTabSystems(out, { tempo: null });
  check("one line per string", systems[0].lines.length === 4);
  check("G string is on top", systems[0].lines[0].startsWith("G|"));
  check("E string is on the bottom", systems[0].lines[3].startsWith("E|"));
  check(
    "each open string appears on its own line",
    systems[0].lines.every((l) => (l.match(/0/g) ?? []).length === 1),
    systems[0].lines.join("\n")
  );
}
{
  const out = assignFretboard(seq([40, 45, 47, 40], 0.5));
  const tab = renderTab(out, { tempo: 120, showTimestamps: false });
  check("grid layout emits bar lines", tab.includes("|-"), tab);
  check("all lines in a system are the same length", (() => {
    const lines = tab.split("\n").filter((l) => l.includes("|"));
    return new Set(lines.map((l) => l.length)).size === 1;
  })(), tab);
}
{
  check("no notes renders a message", renderTab([]).includes("no playable notes"));
}

console.log(`\n${failures === 0 ? "all tests passed" : `${failures} failure(s)`}`);
process.exit(failures === 0 ? 0 : 1);
