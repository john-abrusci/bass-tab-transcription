/** Note event as returned by the Runpod worker. */
export interface Note {
  onset: number; // seconds
  duration: number; // seconds
  midi: number;
  confidence: number;
}

export interface Timings {
  fetch_s?: number;
  model_load_s?: number;
  separation_s: number;
  pitch_s: number;
  total_s: number;
  worker_uptime_at_request_s?: number;
  cold?: boolean;
}

/** The `output` object of the /runsync response. */
export interface TranscriptionOutput {
  notes: Note[];
  tempo_bpm_estimate: number | null;
  duration_s: number;
  timings: Timings;
  stem_wav_b64?: string;
}

export interface TranscribeResponse extends TranscriptionOutput {
  /** Round-trip measured by the Next.js route, including payload transfer. */
  round_trip_s: number;
  source: "runpod" | "mock";
}

/** A string/fret choice on the neck. `string` is 0 = lowest (E). */
export interface Position {
  string: number;
  fret: number;
}

export interface PositionedNote extends Note {
  position: Position | null; // null when no string/fret can produce the pitch
}

export const NOTE_NAMES = [
  "C",
  "C#",
  "D",
  "D#",
  "E",
  "F",
  "F#",
  "G",
  "G#",
  "A",
  "A#",
  "B",
] as const;

export function midiToName(midi: number): string {
  return `${NOTE_NAMES[((midi % 12) + 12) % 12]}${Math.floor(midi / 12) - 1}`;
}
