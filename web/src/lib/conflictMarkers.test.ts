import { describe, expect, it } from "vitest";

import {
  conflictRegions,
  countConflicts,
  hunkEdit,
  parseConflicts,
  resolveAll,
  resolveHunk,
} from "./conflictMarkers";

const DIFF3 = [
  "intro\n",
  "<<<<<<< HEAD\n",
  "# Demo B\n",
  "||||||| 3d3c756\n",
  "# Demo\n",
  "=======\n",
  "# Demo A\n",
  ">>>>>>> main\n",
  "middle\n",
  "<<<<<<< HEAD\n",
  "b()\n",
  "=======\n",
  "a()\n",
  ">>>>>>> main\n",
  "outro",
].join("");

describe("parseConflicts", () => {
  it("splits text and diff3 or merge-style hunks, and round-trips", () => {
    const segments = parseConflicts(DIFF3);

    expect(segments.map((s) => s.kind)).toEqual(["text", "conflict", "text", "conflict", "text"]);
    expect(segments[1]).toMatchObject({
      ours: "# Demo B\n",
      ancestor: "# Demo\n",
      theirs: "# Demo A\n",
      oursLabel: "HEAD",
      theirsLabel: "main",
    });
    expect(segments[3]).toMatchObject({ ours: "b()\n", ancestor: null, theirs: "a()\n" });
    expect(segments.map((s) => (s.kind === "text" ? s.text : s.raw)).join("")).toBe(DIFF3);
  });

  it("keeps an unterminated hunk as plain text", () => {
    const text = "a\n<<<<<<< HEAD\nb\n=======\nc\n";
    expect(parseConflicts(text)).toEqual([{ kind: "text", text }]);
    expect(countConflicts(text)).toBe(0);
  });

  it("handles CRLF files and markers without labels", () => {
    const text = "<<<<<<<\r\nx\r\n=======\r\ny\r\n>>>>>>>\r\n";
    const [hunk] = parseConflicts(text);
    expect(hunk).toMatchObject({ kind: "conflict", ours: "x\r\n", theirs: "y\r\n" });
  });
});

describe("resolveHunk", () => {
  it("replaces one hunk and keeps the others and hand edits", () => {
    const edited = DIFF3.replace("intro", "intro (edited)");

    const first = resolveHunk(edited, 0, "theirs");
    expect(first).toContain("intro (edited)\n# Demo A\nmiddle");
    expect(countConflicts(first)).toBe(1);

    const both = resolveHunk(first, 0, "both");
    expect(both).toBe("intro (edited)\n# Demo A\nmiddle\nb()\na()\noutro");
    expect(countConflicts(both)).toBe(0);
  });

  it("keeps the task's side and ignores an out-of-range hunk", () => {
    expect(resolveHunk(DIFF3, 1, "ours")).toContain("middle\nb()\noutro");
    expect(resolveHunk(DIFF3, 5, "ours")).toBe(DIFF3);
  });
});

describe("resolveAll", () => {
  it("resolves every hunk the same way", () => {
    expect(resolveAll(DIFF3, "ours")).toBe("intro\n# Demo B\nmiddle\nb()\noutro");
    expect(resolveAll(DIFF3, "theirs")).toBe("intro\n# Demo A\nmiddle\na()\noutro");
    expect(countConflicts(resolveAll(DIFF3, "both"))).toBe(0);
  });
});

describe("conflictRegions", () => {
  it("locates each hunk's sections and marker lines", () => {
    expect(conflictRegions(DIFF3)).toEqual([
      {
        hunk: { start: 2, end: 8 },
        ours: { start: 3, end: 3 },
        ancestor: { start: 5, end: 5 },
        theirs: { start: 7, end: 7 },
        markers: [2, 4, 6, 8],
      },
      {
        hunk: { start: 10, end: 14 },
        ours: { start: 11, end: 11 },
        ancestor: null,
        theirs: { start: 13, end: 13 },
        markers: [10, 12, 14],
      },
    ]);
  });

  it("keeps an empty side as an empty range", () => {
    const [region] = conflictRegions("<<<<<<< HEAD\n=======\nx\n>>>>>>> main\n");

    expect(region.ours).toEqual({ start: 2, end: 1 });
    expect(region.theirs).toEqual({ start: 3, end: 3 });
    expect(region.hunk).toEqual({ start: 1, end: 4 });
  });
});

describe("hunkEdit", () => {
  function apply(text: string, edit: NonNullable<ReturnType<typeof hunkEdit>>): string {
    const lines = text.split("\n");
    const offset = (line: number, column: number) =>
      lines.slice(0, line - 1).reduce((n, l) => n + l.length + 1, 0) + column - 1;
    const from = offset(edit.startLine, edit.startColumn);
    const to = offset(edit.endLine, edit.endColumn);
    return text.slice(0, from) + edit.text + text.slice(to);
  }

  it("agrees with resolveHunk for every choice", () => {
    for (const choice of ["ours", "theirs", "both"] as const) {
      for (const index of [0, 1]) {
        const edit = hunkEdit(DIFF3, index, choice);
        expect(edit).not.toBeNull();
        expect(apply(DIFF3, edit!)).toBe(resolveHunk(DIFF3, index, choice));
      }
    }
  });

  it("drops the hunk's lines when the kept side is empty", () => {
    const middle = "a\n<<<<<<< HEAD\n=======\nx\n>>>>>>> main\nb\n";
    expect(apply(middle, hunkEdit(middle, 0, "ours")!)).toBe("a\nb\n");
    const last = "a\n<<<<<<< HEAD\n=======\nx\n>>>>>>> main";
    expect(apply(last, hunkEdit(last, 0, "ours")!)).toBe("a");
  });

  it("returns null for a hunk that is not there", () => {
    expect(hunkEdit("plain\n", 0, "ours")).toBeNull();
  });
});
