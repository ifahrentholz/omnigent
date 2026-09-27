import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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

const step = { mutate: vi.fn(), isPending: false, isError: false, error: null };
const tell = { mutate: vi.fn() };
const onOpenChange = vi.fn();

beforeEach(() => {
  step.mutate.mockReset();
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

describe("ConflictResolverDialog", () => {
  it("starts the merge when none is in progress", () => {
    mergeState({ in_progress: false });
    renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "Start merge of main" }));

    expect(step.mutate).toHaveBeenCalledWith({ action: "start" }, expect.anything());
  });

  it("accepts a side for every hunk and saves the file", async () => {
    mergeState({ conflicts: [readme()] });
    renderDialog();

    const result = await screen.findByLabelText<HTMLTextAreaElement>("Result for README.md");
    expect(screen.getByText("Task (omni/b)")).toBeTruthy();
    expect(result.value).toBe(WORKING);
    expect(screen.getByRole("button", { name: "Mark resolved" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Complete merge" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "main" }));
    expect(result.value).toBe("intro\n# Demo A\noutro\n");

    fireEvent.change(result, { target: { value: "intro\n# Demo A & B\noutro\n" } });
    fireEvent.click(screen.getByRole("button", { name: "Mark resolved" }));

    expect(step.mutate).toHaveBeenCalledWith(
      { action: "resolve", path: "README.md", content: "intro\n# Demo A & B\noutro\n" },
      expect.anything(),
    );
  });

  it("keeps Mark resolved disabled while a marker is left", async () => {
    mergeState({ conflicts: [readme()] });
    renderDialog();

    const result = await screen.findByLabelText<HTMLTextAreaElement>("Result for README.md");
    fireEvent.click(screen.getByRole("button", { name: "Both" }));
    expect(result.value).toBe("intro\n# Demo B\n# Demo A\noutro\n");
    expect(screen.getByRole("button", { name: "Mark resolved" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Both" })).toBeDisabled();
  });

  it("completes the merge and tells the worker", () => {
    mergeState({ conflicts: [] });
    renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "Complete merge" }));
    const [action, options] = step.mutate.mock.calls[0] as [
      unknown,
      { onSuccess: (r: unknown) => void },
    ];
    expect(action).toEqual({ action: "complete" });
    options.onSuccess({ committed: true, commit: "abcdef123", resolved: ["README.md"] });

    const [notice] = tell.mutate.mock.calls[0] as [string];
    expect(notice).toContain("merged `main` into your branch");
    expect(notice).toContain("abcdef1");
    expect(notice).toContain("README.md");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("skips the worker message when opted out, and aborts on request", () => {
    mergeState({ conflicts: [] });
    renderDialog();

    fireEvent.click(screen.getByLabelText("Tell the worker what was resolved"));
    fireEvent.click(screen.getByRole("button", { name: "Complete merge" }));
    const [, options] = step.mutate.mock.calls[0] as [unknown, { onSuccess: (r: unknown) => void }];
    options.onSuccess({ committed: true, commit: "abc", resolved: [] });
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

    fireEvent.click(screen.getByRole("button", { name: "Keep task version" }));

    expect(step.mutate).toHaveBeenCalledWith(
      { action: "resolve", path: "logo.png", side: "ours" },
      expect.anything(),
    );
  });
});
