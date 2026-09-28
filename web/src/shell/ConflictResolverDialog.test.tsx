import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { MergeEditorProps } from "./MergeEditor";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type MergeState, useMergeState, useResolveStep } from "@/hooks/useBranchResolve";
import { useMessageWorker } from "@/hooks/useLandBranch";
import type * as LandBranchModule from "@/hooks/useLandBranch";
import { ConflictResolverDialog } from "./ConflictResolverDialog";

vi.mock("@/hooks/useBranchResolve", () => ({
  useMergeState: vi.fn(),
  useResolveStep: vi.fn(),
}));

// Monaco can't run in jsdom; a textarea stands in for the result editor.
vi.mock("./MergeEditor", () => ({
  default: ({ path, value, onChange, oursLabel }: MergeEditorProps) => (
    <label>
      {oursLabel}
      <textarea
        aria-label={`Result for ${path}`}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  ),
}));

vi.mock("@/hooks/useLandBranch", async (importOriginal) => ({
  ...(await importOriginal<typeof LandBranchModule>()),
  useMessageWorker: vi.fn(),
}));

const WORKING = [
  "intro\n",
  "<<<<<<< HEAD\n",
  "# Demo B\n",
  "||||||| base\n",
  "# Demo\n",
  "=======\n",
  "# Demo A\n",
  ">>>>>>> main\n",
  "outro\n",
].join("");

function readme(overrides: Partial<MergeState["conflicts"][number]> = {}) {
  return {
    path: "README.md",
    binary: false,
    ours: "intro\n# Demo B\noutro\n",
    theirs: "intro\n# Demo A\noutro\n",
    ancestor: "intro\n# Demo\noutro\n",
    working: WORKING,
    deleted_in_ours: false,
    deleted_in_theirs: false,
    ...overrides,
  };
}

function mergeState(state: Partial<MergeState> | null) {
  vi.mocked(useMergeState).mockReturnValue({
    data: state
      ? { in_progress: true, base: "main", branch: "omni/b", conflicts: [], ...state }
      : undefined,
    isLoading: false,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof useMergeState>);
}

const step = {
  mutate: vi.fn(),
  mutateAsync: vi.fn(),
  isPending: false,
  isError: false,
  error: null,
};
const tell = { mutate: vi.fn() };
const onOpenChange = vi.fn();

beforeEach(() => {
  step.mutate.mockReset();
  step.mutateAsync.mockReset();
  tell.mutate.mockReset();
  onOpenChange.mockReset();
  vi.mocked(useResolveStep).mockReturnValue(step as unknown as ReturnType<typeof useResolveStep>);
  vi.mocked(useMessageWorker).mockReturnValue(
    tell as unknown as ReturnType<typeof useMessageWorker>,
  );
});

afterEach(cleanup);

function renderDialog() {
  render(
    <ConflictResolverDialog
      sessionId="conv_b"
      branch="omni/b"
      base="main"
      open
      onOpenChange={onOpenChange}
    />,
  );
}

const APP = "export const a = 1;\n";

function app() {
  return readme({
    path: "src/app.ts",
    ours: APP,
    theirs: APP,
    ancestor: APP,
    working: "<<<<<<< HEAD\nexport const a = 2;\n=======\nexport const a = 3;\n>>>>>>> main\n",
  });
}

async function result(path: string) {
  return screen.findByLabelText<HTMLTextAreaElement>(`Result for ${path}`);
}

describe("ConflictResolverDialog", () => {
  it("starts the merge when none is in progress", () => {
    mergeState({ in_progress: false });
    renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "Start merge" }));

    expect(step.mutate).toHaveBeenCalledWith({ action: "start" }, expect.anything());
  });

  it("shows progress instead of a dead button while conflicts are left", async () => {
    mergeState({ conflicts: [readme(), app()] });
    renderDialog();

    expect((await result("README.md")).value).toBe(WORKING);
    expect(screen.getByText("Task (omni/b)")).toBeTruthy();
    expect(screen.getByTestId("merge-progress").textContent).toBe(
      "1 conflict left in README.md · 2 files to go",
    );
    expect(
      screen.queryByRole("button", { name: /Commit merge|Next file|Mark resolved/ }),
    ).toBeNull();
    expect(screen.getAllByTestId("file-status").map((el) => el.textContent)).toEqual(["1", "1"]);
  });

  it("moves on to the next file once the current one is resolved", async () => {
    mergeState({ conflicts: [readme(), app()] });
    renderDialog();

    fireEvent.change(await result("README.md"), {
      target: { value: "intro\n# Demo A\noutro\n" },
    });
    expect(screen.getAllByTestId("file-status").map((el) => el.textContent)).toEqual(["done", "1"]);
    fireEvent.click(screen.getByRole("button", { name: "Next file: src/app.ts" }));

    expect((await result("src/app.ts")).value).toContain("<<<<<<< HEAD");
    expect(screen.getByTestId("merge-progress").textContent).toBe("1 conflict left in src/app.ts");
  });

  it("saves every file, commits and tells the worker in one step", async () => {
    mergeState({ conflicts: [readme(), app()] });
    step.mutateAsync.mockImplementation((action: { action: string }) =>
      Promise.resolve(
        action.action === "complete"
          ? { committed: true, commit: "abcdef123", resolved: ["README.md", "src/app.ts"] }
          : { in_progress: true, conflicts: [] },
      ),
    );
    renderDialog();

    fireEvent.change(await result("README.md"), { target: { value: "intro\n# Both\noutro\n" } });
    fireEvent.click(screen.getByRole("button", { name: "Next file: src/app.ts" }));
    fireEvent.change(await result("src/app.ts"), { target: { value: "export const a = 3;\n" } });
    expect(screen.getByTestId("merge-progress").textContent).toBe("All conflicts resolved.");
    fireEvent.click(screen.getByRole("button", { name: "Commit merge" }));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(step.mutateAsync.mock.calls.map(([action]) => action)).toEqual([
      { action: "resolve", path: "README.md", content: "intro\n# Both\noutro\n" },
      { action: "resolve", path: "src/app.ts", content: "export const a = 3;\n" },
      { action: "complete" },
    ]);
    const [notice] = tell.mutate.mock.calls[0] as [string];
    expect(notice).toContain("merged `main` into your branch");
    expect(notice).toContain("abcdef1");
  });

  it("skips the worker message when opted out, and aborts on request", async () => {
    mergeState({ conflicts: [] });
    step.mutateAsync.mockResolvedValue({ committed: true, commit: "abc", resolved: [] });
    renderDialog();

    fireEvent.click(screen.getByLabelText("Tell the worker"));
    fireEvent.click(screen.getByRole("button", { name: "Commit merge" }));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(tell.mutate).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Abort merge" }));
    expect(step.mutate).toHaveBeenLastCalledWith({ action: "abort" }, expect.anything());
  });

  it("offers whole-file choices for a binary conflict", () => {
    mergeState({
      conflicts: [
        readme({ path: "logo.png", binary: true, working: null, ours: null, theirs: null }),
      ],
    });
    renderDialog();

    expect(screen.getByTestId("file-status").textContent).toBe("pick a side");
    fireEvent.click(screen.getByRole("button", { name: "Keep task version" }));

    expect(step.mutate).toHaveBeenCalledWith(
      { action: "resolve", path: "logo.png", side: "ours" },
      expect.anything(),
    );
  });
});
