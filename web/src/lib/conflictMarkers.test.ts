import { describe, expect, it } from "vitest";

import { countConflicts, parseConflicts, resolveHunk } from "./conflictMarkers";

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
