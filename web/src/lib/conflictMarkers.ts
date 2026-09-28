// Parse and resolve git conflict markers (merge or diff3 style) in a file.
//
// A conflicted file alternates plain text and conflict hunks:
//
//   <<<<<<< ours-label
//   task's lines
//   ||||||| ancestor-label      (diff3 only)
//   common ancestor's lines
//   =======
//   base's lines
//   >>>>>>> theirs-label
//
// The resolver keeps the result as editable text and replaces one hunk at a
// time, so hand edits elsewhere in the file survive a later choice.

export type ConflictSegment =
  | { kind: "text"; text: string }
  | {
      kind: "conflict";
      ours: string;
      ancestor: string | null;
      theirs: string;
      oursLabel: string;
      theirsLabel: string;
      /** The hunk exactly as written, markers included. */
      raw: string;
    };

export type HunkChoice = "ours" | "theirs" | "both";

const OURS = "<<<<<<<";
const ANCESTOR = "|||||||";
const SPLIT = "=======";
const THEIRS = ">>>>>>>";

/** Whether `line` (without its newline) is the marker `marker`, optionally labelled. */
function isMarker(line: string, marker: string): boolean {
  return line === marker || line.startsWith(`${marker} `);
}

function label(line: string, marker: string): string {
  return line.slice(marker.length).trim();
}

/**
 * Split a file into text and conflict segments.
 *
 * An unterminated hunk (no closing `>>>>>>>`) stays plain text, so a stray
 * marker never swallows the rest of the file.
 *
 * @param text - File content, possibly with conflict markers.
 * @returns Segments in file order; joining their text restores `text`.
 */
export function parseConflicts(text: string): ConflictSegment[] {
  const lines = text.match(/[^\n]*\n|[^\n]+$/g) ?? [];
  const segments: ConflictSegment[] = [];
  let plain = "";
  let i = 0;
  while (i < lines.length) {
    const line = lines[i].replace(/\r?\n$/, "");
    if (!isMarker(line, OURS)) {
      plain += lines[i];
      i += 1;
      continue;
    }
    const parts: Record<"ours" | "ancestor" | "theirs", string> = {
      ours: "",
      ancestor: "",
      theirs: "",
    };
    let hasAncestor = false;
    let section: keyof typeof parts = "ours";
    let raw = lines[i];
    let theirsLabel = "";
    let closed = false;
    let j = i + 1;
    for (; j < lines.length; j += 1) {
      const current = lines[j].replace(/\r?\n$/, "");
      raw += lines[j];
      if (section === "ours" && isMarker(current, ANCESTOR)) {
        section = "ancestor";
        hasAncestor = true;
      } else if (section !== "theirs" && current === SPLIT) {
        section = "theirs";
      } else if (section === "theirs" && isMarker(current, THEIRS)) {
        theirsLabel = label(current, THEIRS);
        closed = true;
        break;
      } else {
        parts[section] += lines[j];
      }
    }
    if (!closed) {
      plain += raw;
      i = j;
      continue;
    }
    if (plain) segments.push({ kind: "text", text: plain });
    plain = "";
    segments.push({
      kind: "conflict",
      ours: parts.ours,
      ancestor: hasAncestor ? parts.ancestor : null,
      theirs: parts.theirs,
      oursLabel: label(line, OURS),
      theirsLabel,
      raw,
    });
    i = j + 1;
  }
  if (plain) segments.push({ kind: "text", text: plain });
  return segments;
}

/** Number of unresolved conflict hunks in `text`. */
export function countConflicts(text: string): number {
  return parseConflicts(text).filter((segment) => segment.kind === "conflict").length;
}

/**
 * Replace the `index`-th conflict hunk of `text` with the chosen side(s).
 *
 * @param text - Current (partially resolved) file content.
 * @param index - Which remaining hunk, counting from 0.
 * @param choice - Keep the task's lines, the base's, or both (task first).
 * @returns The content with that hunk resolved; unchanged for a bad index.
 */
export function resolveHunk(text: string, index: number, choice: HunkChoice): string {
  let seen = -1;
  return parseConflicts(text)
    .map((segment) => {
      if (segment.kind === "text") return segment.text;
      seen += 1;
      if (seen !== index) return segment.raw;
      if (choice === "ours") return segment.ours;
      if (choice === "theirs") return segment.theirs;
      return segment.ours + segment.theirs;
    })
    .join("");
}

/** Resolve every remaining hunk of `text` the same way. */
export function resolveAll(text: string, choice: HunkChoice): string {
  return parseConflicts(text)
    .map((segment) => {
      if (segment.kind === "text") return segment.text;
      if (choice === "ours") return segment.ours;
      if (choice === "theirs") return segment.theirs;
      return segment.ours + segment.theirs;
    })
    .join("");
}

/** Inclusive 1-based line range; `end < start` means the section is empty. */
export interface LineRange {
  start: number;
  end: number;
}

/** Where one conflict hunk sits in the file, by line. */
export interface ConflictRegion {
  /** The whole hunk, markers included. */
  hunk: LineRange;
  ours: LineRange;
  /** Null for merge-style markers without a common-ancestor section. */
  ancestor: LineRange | null;
  theirs: LineRange;
  /** The marker lines themselves: `<<<<<<<`, `|||||||`, `=======`, `>>>>>>>`. */
  markers: number[];
}

function lineCount(text: string): number {
  if (!text) return 0;
  const breaks = text.split("\n").length - 1;
  return text.endsWith("\n") ? breaks : breaks + 1;
}

/**
 * Line positions of every conflict hunk, in file order.
 *
 * @param text - File content, possibly with conflict markers.
 * @returns One region per hunk that {@link parseConflicts} recognises.
 */
export function conflictRegions(text: string): ConflictRegion[] {
  const regions: ConflictRegion[] = [];
  let line = 1;
  for (const segment of parseConflicts(text)) {
    if (segment.kind === "text") {
      line += lineCount(segment.text);
      continue;
    }
    const start = line;
    const markers = [start];
    const ours = { start: start + 1, end: start + lineCount(segment.ours) };
    let next = ours.end + 1;
    let ancestor: LineRange | null = null;
    if (segment.ancestor !== null) {
      markers.push(next);
      ancestor = { start: next + 1, end: next + lineCount(segment.ancestor) };
      next = ancestor.end + 1;
    }
    markers.push(next);
    const theirs = { start: next + 1, end: next + lineCount(segment.theirs) };
    const end = theirs.end + 1;
    markers.push(end);
    regions.push({ hunk: { start, end }, ours, ancestor, theirs, markers });
    line = end + 1;
  }
  return regions;
}

/** A replacement over 1-based line/column positions, as Monaco edits take it. */
export interface TextEdit {
  startLine: number;
  startColumn: number;
  endLine: number;
  endColumn: number;
  text: string;
}

/**
 * The edit that resolves hunk `index` in place, touching only its lines so
 * the rest of the file (and an editor's undo history) is kept.
 *
 * @returns The edit, or null for a bad index.
 */
export function hunkEdit(text: string, index: number, choice: HunkChoice): TextEdit | null {
  const region = conflictRegions(text)[index];
  const hunk = parseConflicts(text).filter((s) => s.kind === "conflict")[index];
  if (!region || !hunk || hunk.kind !== "conflict") return null;
  const kept =
    choice === "ours" ? hunk.ours : choice === "theirs" ? hunk.theirs : hunk.ours + hunk.theirs;
  const lines = text.split("\n").map((line) => line.replace(/\r$/, ""));
  const endOf = (line: number) => lines[line - 1].length + 1;
  const { start, end } = region.hunk;
  if (kept) {
    const body = kept.replace(/\r?\n$/, "");
    return { startLine: start, startColumn: 1, endLine: end, endColumn: endOf(end), text: body };
  }
  // Nothing kept: drop the hunk's lines together with one line break.
  if (end < lines.length) {
    return { startLine: start, startColumn: 1, endLine: end + 1, endColumn: 1, text: "" };
  }
  if (start > 1) {
    return {
      startLine: start - 1,
      startColumn: endOf(start - 1),
      endLine: end,
      endColumn: endOf(end),
      text: "",
    };
  }
  return { startLine: start, startColumn: 1, endLine: end, endColumn: endOf(end), text: "" };
}
