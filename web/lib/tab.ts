/**
 * Phase 4 -- render positioned notes as ASCII tab.
 *
 * Two layout modes:
 *  - grid, when we have a tempo estimate: notes snap to a subdivision grid and
 *    get bar lines, so the result reads like a tab someone wrote.
 *  - sequential, when we don't: one column per note, in order. Honest about
 *    the fact that we know the order but not the meter.
 */

import { STRING_LABELS, STANDARD_TUNING } from "./fretboard";
import type { PositionedNote } from "./types";

export interface TabOptions {
  tempo?: number | null;
  /** Columns per beat. 4 = sixteenth-note grid. */
  subdivision?: number;
  beatsPerBar?: number;
  barsPerLine?: number;
  /** Columns per line in sequential mode. */
  columnsPerLine?: number;
  labels?: string[];
  /** Print the start time of each system above it. */
  showTimestamps?: boolean;
}

export interface TabSystem {
  startTime: number;
  lines: string[];
}

interface Cell {
  fret: number;
  string: number;
}

const DEFAULTS = {
  subdivision: 4,
  beatsPerBar: 4,
  barsPerLine: 2,
  columnsPerLine: 20,
  showTimestamps: true,
};

/**
 * Place notes into columns. Grid mode snaps to the subdivision; when two notes
 * land in the same column the later one is bumped right, because a 4-string
 * bass line is overwhelmingly monophonic and a collision means the grid is too
 * coarse, not that a chord was played.
 */
function layout(
  notes: PositionedNote[],
  colDuration: number | null
): { cols: (Cell | null)[]; colTimes: number[] } {
  const cols: (Cell | null)[] = [];
  const colTimes: number[] = [];
  const playable = notes.filter((n) => n.position !== null);

  if (colDuration === null) {
    for (const n of playable) {
      cols.push({ fret: n.position!.fret, string: n.position!.string });
      colTimes.push(n.onset);
    }
    return { cols, colTimes };
  }

  for (const n of playable) {
    let idx = Math.round(n.onset / colDuration);
    while (cols[idx]) idx++;
    while (cols.length <= idx) {
      colTimes[cols.length] = cols.length * colDuration;
      cols.push(null);
    }
    cols[idx] = { fret: n.position!.fret, string: n.position!.string };
    colTimes[idx] = idx * colDuration;
  }
  return { cols, colTimes };
}

export function renderTabSystems(
  notes: PositionedNote[],
  options: TabOptions = {}
): TabSystem[] {
  const o = { ...DEFAULTS, ...options };
  const labels = o.labels ?? STRING_LABELS;
  const nStrings = labels.length;

  const grid = !!o.tempo && o.tempo > 0;
  const colDuration = grid ? 60 / (o.tempo as number) / o.subdivision : null;
  const { cols, colTimes } = layout(notes, colDuration);
  if (cols.length === 0) return [];

  const colsPerBar = o.subdivision * o.beatsPerBar;
  const colsPerLine = grid ? colsPerBar * o.barsPerLine : o.columnsPerLine;

  // Pad grid mode out to a whole number of bars so the last line closes cleanly.
  const totalCols = grid
    ? Math.ceil(cols.length / colsPerBar) * colsPerBar
    : cols.length;

  const labelWidth = Math.max(...labels.map((l) => l.length));
  const systems: TabSystem[] = [];

  for (let start = 0; start < totalCols; start += colsPerLine) {
    const end = Math.min(start + colsPerLine, totalCols);
    const slice = cols.slice(start, end);

    // Column width is set by the widest fret number on this line only, so a
    // single 12th-fret note does not widen the whole piece.
    const width = Math.max(
      1,
      ...slice.map((c) => (c ? String(c.fret).length : 1))
    );

    const lines: string[] = [];
    for (let s = nStrings - 1; s >= 0; s--) {
      let line = `${labels[s].padEnd(labelWidth)}|`;
      for (let c = start; c < end; c++) {
        const cell = cols[c];
        const text =
          cell && cell.string === s ? String(cell.fret).padStart(width, "-") : "-".repeat(width);
        line += text + "-";
        if (grid && (c + 1) % colsPerBar === 0 && c + 1 < end) line += "|-";
      }
      lines.push(line + "|");
    }

    systems.push({ startTime: colTimes[start] ?? 0, lines });
  }

  return systems;
}

export function renderTab(notes: PositionedNote[], options: TabOptions = {}): string {
  const o = { ...DEFAULTS, ...options };
  const systems = renderTabSystems(notes, options);
  if (systems.length === 0) return "(no playable notes)";
  return systems
    .map((sys) => {
      const head = o.showTimestamps ? `${formatTime(sys.startTime)}\n` : "";
      return head + sys.lines.join("\n");
    })
    .join("\n\n");
}

export function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, "0")}`;
}

/** Sanity check that an assignment actually sounds the notes it claims to. */
export function verifyPositions(
  notes: PositionedNote[],
  tuning: number[] = STANDARD_TUNING
): string[] {
  const errors: string[] = [];
  for (const n of notes) {
    if (!n.position) continue;
    const sounded = tuning[n.position.string] + n.position.fret;
    if (sounded !== n.midi) {
      errors.push(
        `note at ${n.onset.toFixed(3)}s: midi ${n.midi} but string ${n.position.string} fret ${n.position.fret} sounds ${sounded}`
      );
    }
  }
  return errors;
}
