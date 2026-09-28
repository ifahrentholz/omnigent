import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import MergeEditor from "./MergeEditor";

// Capture the editors' props; the fake editor below stands in for Monaco.
const h = vi.hoisted(() => ({
  editorProps: null as null | {
    value: string;
    onMount: (editor: unknown, monaco: unknown) => void;
  },
  diffProps: null as null | { original: string; modified: string },
}));

vi.mock("@monaco-editor/react", () => ({
  Editor: (props: NonNullable<typeof h.editorProps>) => {
    h.editorProps = props;
    return null;
  },
  DiffEditor: (props: NonNullable<typeof h.diffProps>) => {
    h.diffProps = props;
    return null;
  },
}));

vi.mock("./monacoSetup", () => ({
  ensureMonacoReady: vi.fn(() => Promise.resolve()),
  ensureLanguage: vi.fn(() => Promise.resolve()),
  monacoLanguageId: vi.fn((lang: string) => lang),
  resolvedThemeToMonaco: vi.fn(() => "github-dark"),
}));

vi.mock("@/components/theme/useResolvedThemeMode", () => ({
  useResolvedThemeMode: () => "dark",
}));

const WORKING = "intro\n<<<<<<< HEAD\n# Demo E\n=======\n# Demo D\n>>>>>>> main\noutro\n";

class Range {
  startLineNumber: number;
  startColumn: number;
  endLineNumber: number;
  endColumn: number;
  constructor(startLine: number, startColumn: number, endLine: number, endColumn: number) {
    this.startLineNumber = startLine;
    this.startColumn = startColumn;
    this.endLineNumber = endLine;
    this.endColumn = endColumn;
  }
}

function fakeMonaco() {
  const commands: ((accessor: unknown, index: number) => void)[] = [];
  const decorations = { set: vi.fn() };
  let provider: {
    provideCodeLenses: (model: unknown) => {
      lenses: { range: Range; command: { id: string; title: string; arguments: unknown[] } }[];
    };
  } | null = null;
  const model = { getValue: () => WORKING };
  const editor = {
    getValue: () => WORKING,
    getModel: () => model,
    createDecorationsCollection: vi.fn(() => decorations),
    addCommand: (_key: number, handler: (typeof commands)[number]) => {
      commands.push(handler);
      return `cmd-${commands.length - 1}`;
    },
    onDidChangeModelContent: () => ({ dispose: () => {} }),
    revealLineInCenter: vi.fn(),
    setPosition: vi.fn(),
    getPosition: () => ({ lineNumber: 1 }),
    pushUndoStop: vi.fn(),
    executeEdits: vi.fn(),
    focus: vi.fn(),
    updateOptions: vi.fn(),
  };
  const monaco = {
    Range,
    Emitter: class {
      event = () => ({ dispose: () => {} });
      fire() {}
      dispose() {}
    },
    editor: { OverviewRulerLane: { Full: 7 } },
    languages: {
      registerCodeLensProvider: (_selector: string, p: NonNullable<typeof provider>) => {
        provider = p;
        return { dispose: () => {} };
      },
    },
  };
  return { editor, monaco, model, commands, lenses: () => provider!.provideCodeLenses(model) };
}

afterEach(cleanup);

async function renderEditor(onChange: (text: string) => void = () => {}, value = WORKING) {
  render(
    <MergeEditor
      path="README.md"
      ours={"intro\n# Demo E\noutro\n"}
      theirs={"intro\n# Demo D\noutro\n"}
      oursLabel="Task (omni/e)"
      base="main"
      value={value}
      onChange={onChange}
    />,
  );
  await screen.findByText("Base (main)");
  const fake = fakeMonaco();
  act(() => h.editorProps!.onMount(fake.editor, fake.monaco));
  return fake;
}

describe("MergeEditor", () => {
  it("diffs the task's version against the base's", async () => {
    await renderEditor();

    expect(h.diffProps).toMatchObject({
      original: "intro\n# Demo E\noutro\n",
      modified: "intro\n# Demo D\noutro\n",
    });
    expect(screen.getByText("Task (omni/e)")).toBeTruthy();
    expect(screen.getByText(/Pick a side with the links/)).toBeTruthy();
  });

  it("offers accept lenses above each hunk and applies the choice in place", async () => {
    const fake = await renderEditor();

    const { lenses } = fake.lenses();
    expect(lenses.map((lens) => lens.command.title)).toEqual([
      "Accept task",
      "Accept main",
      "Accept both",
    ]);
    expect(lenses[0].range.startLineNumber).toBe(2);

    const [id, index] = [lenses[1].command.id, lenses[1].command.arguments[0] as number];
    fake.commands[Number(id.replace("cmd-", ""))](undefined, index);

    expect(fake.editor.executeEdits).toHaveBeenCalledWith("merge-conflict", [
      { range: new Range(2, 1, 6, 13), text: "# Demo D" },
    ]);
  });

  it("colors the task, base and marker lines", async () => {
    const fake = await renderEditor();

    const [initial] = fake.editor.createDecorationsCollection.mock.calls[0] as unknown as [
      { range: Range; options: { className: string } }[],
    ];
    const byClass = Object.fromEntries(
      initial.map((d) => [d.options.className, d.range.startLineNumber]),
    );
    expect(byClass).toMatchObject({
      "merge-marker merge-marker-ours": 2,
      "merge-ours": 3,
      "merge-marker": 4,
      "merge-theirs": 5,
      "merge-marker merge-marker-theirs": 6,
    });
  });

  it("jumps to the next conflict", async () => {
    const fake = await renderEditor();

    fireEvent.click(screen.getByRole("button", { name: "Next conflict" }));

    expect(fake.editor.setPosition).toHaveBeenCalledWith({ lineNumber: 2, column: 1 });
  });

  it("accepts one side for every hunk from the header", async () => {
    const onChange = vi.fn();
    await renderEditor(onChange);

    fireEvent.click(screen.getByRole("button", { name: "main" }));

    expect(onChange).toHaveBeenCalledWith("intro\n# Demo D\noutro\n");
  });

  it("drops the conflict tools once the result is clean", async () => {
    await renderEditor(() => {}, "intro\n# Demo D\noutro\n");

    expect(screen.getByText(/No conflicts left/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Next conflict" })).toBeNull();
    expect(screen.queryByText("Accept all:")).toBeNull();
  });
});
