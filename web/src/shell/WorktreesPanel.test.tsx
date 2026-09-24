import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  findOverlaps,
  useBranchChanges,
  useBranchChangesForSessions,
  useBranchConflicts,
} from "@/hooks/useBranchDiff";
import type * as BranchDiffModule from "@/hooks/useBranchDiff";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import {
  LandError,
  useLandBranch,
  useNotifyOrchestrator,
  useRequestPullRequest,
} from "@/hooks/useLandBranch";
import type * as LandBranchModule from "@/hooks/useLandBranch";
import { WorktreesPanel, conflictNotice, taskStage } from "./WorktreesPanel";

vi.mock("@/hooks/useBranchDiff", async (importOriginal) => ({
  ...(await importOriginal<typeof BranchDiffModule>()),
  useBranchChanges: vi.fn(),
  useBranchChangesForSessions: vi.fn(),
  useBranchConflicts: vi.fn(),
}));

vi.mock("@/hooks/useLandBranch", async (importOriginal) => ({
  ...(await importOriginal<typeof LandBranchModule>()),
  useLandBranch: vi.fn(),
  useRequestPullRequest: vi.fn(),
  useNotifyOrchestrator: vi.fn(),
}));

function mutation(overrides: Record<string, unknown> = {}) {
  return {
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isSuccess: false,
    isError: false,
    data: undefined,
    error: null,
    ...overrides,
  };
}

beforeEach(() => {
  // Default: the panel-level overlap scan sees what each row sees.
  vi.mocked(useBranchChangesForSessions).mockImplementation(
    (ids: string[]) =>
      ids.map((id) => vi.mocked(useBranchChanges)(id)) as unknown as ReturnType<
        typeof useBranchChangesForSessions
      >,
  );
  vi.mocked(useLandBranch).mockReturnValue(
    mutation() as unknown as ReturnType<typeof useLandBranch>,
  );
  vi.mocked(useRequestPullRequest).mockReturnValue(
    mutation() as unknown as ReturnType<typeof useRequestPullRequest>,
  );
  vi.mocked(useNotifyOrchestrator).mockReturnValue(
    mutation() as unknown as ReturnType<typeof useNotifyOrchestrator>,
  );
  vi.mocked(useBranchConflicts).mockReturnValue(verdicts(null));
});

function verdicts(
  results: { ref: string; clean: boolean | null; files: string[] }[] | null,
  dirty = false,
) {
  return {
    data: results ? { base: "main", dirty, supported: true, results } : undefined,
  } as unknown as ReturnType<typeof useBranchConflicts>;
}

function sharedFileChanges() {
  vi.mocked(useBranchChanges).mockImplementation(
    (id: string | undefined) =>
      (id === "conv_a"
        ? branchResult([{ path: "src/shared.ts", added: 1, removed: 0 }])
        : branchResult([{ path: "src/shared.ts", added: 2, removed: 1 }])) as ReturnType<
        typeof useBranchChanges
      >,
  );
}

const PAIR = [
  child({ id: "conv_a", session_name: "login", git_branch: "omni/login-1" }),
  child({ id: "conv_b", session_name: "billing", git_branch: "omni/billing-2" }),
];

afterEach(cleanup);

function child(overrides: Partial<ChildSessionInfo>): ChildSessionInfo {
  return {
    id: "conv_child",
    title: "worker:login",
    task_summary: null,
    tool: "worker",
    session_name: "login",
    current_task_status: null,
    busy: false,
    last_message_preview: null,
    pending_elicitations_count: 0,
    workspace: null,
    git_branch: null,
    git_base_branch: null,
    ...overrides,
  };
}

function branchResult(
  files: { path: string; added: number; removed: number }[],
  ports: { index: number; base: number; span: number } | null = null,
) {
  return {
    data: {
      available: true,
      reason: null,
      base: "main",
      mergeBase: "abc",
      ports,
      data: files.map((file) => ({
        path: file.path,
        name: file.path,
        status: "modified",
        renamed: false,
        previous_path: null,
        bytes: 1,
        modified_at: null,
        lines_added: file.added,
        lines_removed: file.removed,
      })),
    },
    isLoading: false,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof useBranchChanges>;
}

describe("WorktreesPanel", () => {
  it("lists only worktree children with their branch totals", () => {
    vi.mocked(useBranchChanges).mockReturnValue(
      branchResult([
        { path: "src/a.ts", added: 3, removed: 1 },
        { path: "src/b.ts", added: 2, removed: 0 },
      ]),
    );
    render(
      <MemoryRouter>
        <WorktreesPanel
          conversationId="conv_root"
          sessions={[
            child({ id: "conv_wt", git_branch: "omni/login-a1b2c3", git_base_branch: "main" }),
            child({ id: "conv_shared", title: "worker:docs", session_name: "docs" }),
          ]}
        />
      </MemoryRouter>,
    );

    const rows = screen.getAllByTestId("worktree-row");
    expect(rows).toHaveLength(1);
    expect(rows[0].getAttribute("data-child-session-id")).toBe("conv_wt");
    expect(screen.getByText("omni/login-a1b2c3")).toBeTruthy();
    expect(screen.getByText("+5")).toBeTruthy();
    expect(screen.getByText("−1")).toBeTruthy();
    expect(screen.getByText("2 files")).toBeTruthy();
    expect(vi.mocked(useBranchChanges)).toHaveBeenCalledWith("conv_wt");
  });

  it("shows the worktree's dev server port range when one is allocated", () => {
    vi.mocked(useBranchChanges).mockReturnValue(
      branchResult([], { index: 2, base: 3020, span: 10 }),
    );
    render(
      <MemoryRouter>
        <WorktreesPanel
          conversationId="conv_root"
          sessions={[child({ id: "conv_wt", git_branch: "omni/login-a1b2c3" })]}
        />
      </MemoryRouter>,
    );

    const ports = screen.getByTestId("worktree-ports");
    expect(ports.textContent).toBe(":3020–3029");
    expect(ports.getAttribute("title")).toContain("PORT=3020");
  });

  it("opens a file straight into the child's branch diff", () => {
    vi.mocked(useBranchChanges).mockReturnValue(
      branchResult([{ path: "src/a.ts", added: 1, removed: 0 }]),
    );
    render(
      <MemoryRouter initialEntries={["/c/conv_root?debug=1&file=stale.ts"]}>
        <WorktreesPanel
          conversationId="conv_root"
          sessions={[child({ id: "conv_wt", git_branch: "omni/login-a1b2c3" })]}
        />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Expand login" }));
    const link = screen.getByTestId("worktree-file");
    const href = new URL(link.getAttribute("href") ?? "", "http://x");
    expect(href.pathname).toBe("/c/conv_wt");
    expect(href.searchParams.get("file")).toBe("src/a.ts");
    expect(href.searchParams.get("diff")).toBe("1");
    expect(href.searchParams.get("diffsrc")).toBe("branch");
  });

  it("explains how worktrees appear when there are none", () => {
    render(
      <MemoryRouter>
        <WorktreesPanel conversationId="conv_root" sessions={[child({})]} />
      </MemoryRouter>,
    );
    expect(screen.getByText(/No sub-agent worktrees yet/)).toBeTruthy();
  });

  it("lands the branch with the chosen strategy", () => {
    const land = mutation();
    vi.mocked(useLandBranch).mockReturnValue(land as unknown as ReturnType<typeof useLandBranch>);
    vi.mocked(useBranchChanges).mockReturnValue(branchResult([]));
    render(
      <MemoryRouter>
        <WorktreesPanel
          conversationId="conv_root"
          sessions={[child({ id: "conv_wt", git_branch: "omni/login-a1b2c3" })]}
        />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Expand login" }));
    fireEvent.click(screen.getByRole("button", { name: "Land…" }));
    fireEvent.change(screen.getByLabelText("Merge strategy"), { target: { value: "squash" } });
    fireEvent.click(screen.getByRole("button", { name: "Land into main" }));

    expect(vi.mocked(useLandBranch)).toHaveBeenCalledWith("conv_wt");
    expect(land.mutate).toHaveBeenCalledWith({ strategy: "squash" }, expect.anything());
  });

  it("lists the conflicting files of a refused merge", () => {
    vi.mocked(useLandBranch).mockReturnValue(
      mutation({
        isError: true,
        error: new LandError("merging conflicts; nothing was changed", ["src/app.py"]),
      }) as unknown as ReturnType<typeof useLandBranch>,
    );
    vi.mocked(useBranchChanges).mockReturnValue(branchResult([]));
    render(
      <MemoryRouter>
        <WorktreesPanel
          conversationId="conv_root"
          sessions={[child({ id: "conv_wt", git_branch: "omni/login-a1b2c3" })]}
        />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Expand login" }));
    expect(screen.getByText("merging conflicts; nothing was changed")).toBeTruthy();
    expect(screen.getByText("src/app.py")).toBeTruthy();
  });

  it("flags files that another parallel task changes too", () => {
    vi.mocked(useBranchChanges).mockImplementation(
      (id: string | undefined) =>
        (id === "conv_a"
          ? branchResult([
              { path: "src/shared.ts", added: 1, removed: 0 },
              { path: "src/only_a.ts", added: 1, removed: 0 },
            ])
          : branchResult([{ path: "src/shared.ts", added: 2, removed: 1 }])) as ReturnType<
          typeof useBranchChanges
        >,
    );
    render(
      <MemoryRouter>
        <WorktreesPanel
          conversationId="conv_root"
          sessions={[
            child({ id: "conv_a", session_name: "login", git_branch: "omni/login-1" }),
            child({ id: "conv_b", session_name: "billing", git_branch: "omni/billing-2" }),
          ]}
        />
      </MemoryRouter>,
    );

    const badges = screen.getAllByTestId("worktree-overlap");
    expect(badges).toHaveLength(2);
    expect(badges[0].getAttribute("title")).toContain("billing");
    fireEvent.click(screen.getByRole("button", { name: "Expand login" }));
    expect(screen.getAllByLabelText("changed by another task too")).toHaveLength(1);
  });
});

describe("conflict prediction", () => {
  it("dry-runs each task against its base and the tasks sharing its files", () => {
    sharedFileChanges();
    render(
      <MemoryRouter>
        <WorktreesPanel conversationId="conv_root" sessions={PAIR} orchestratorId="conv_root" />
      </MemoryRouter>,
    );
    expect(vi.mocked(useBranchConflicts)).toHaveBeenCalledWith("conv_a", ["omni/billing-2"], {
      enabled: true,
    });
    expect(vi.mocked(useBranchConflicts)).toHaveBeenCalledWith("conv_b", ["omni/login-1"], {
      enabled: true,
    });
  });

  it("tells a clean overlap from a real conflict", () => {
    sharedFileChanges();
    vi.mocked(useBranchConflicts).mockImplementation((id) =>
      id === "conv_a"
        ? verdicts([
            { ref: "main", clean: true, files: [] },
            { ref: "omni/billing-2", clean: false, files: ["src/shared.ts"] },
          ])
        : verdicts([
            { ref: "main", clean: true, files: [] },
            { ref: "omni/login-1", clean: true, files: [] },
          ]),
    );
    render(
      <MemoryRouter>
        <WorktreesPanel conversationId="conv_root" sessions={PAIR} orchestratorId="conv_root" />
      </MemoryRouter>,
    );

    const [conflicting, clean] = screen.getAllByTestId("worktree-overlap");
    expect(conflicting.textContent).toBe("conflict");
    expect(conflicting.getAttribute("title")).toContain("omni/billing-2");
    expect(clean.textContent).toBe("1 shared");
    expect(clean.getAttribute("title")).toContain("predicted to merge cleanly");
  });

  it("flags a conflict with the base and lets the user notify the orchestrator", () => {
    const notify = mutation();
    vi.mocked(useNotifyOrchestrator).mockReturnValue(
      notify as unknown as ReturnType<typeof useNotifyOrchestrator>,
    );
    vi.mocked(useBranchChanges).mockReturnValue(
      branchResult([{ path: "src/a.ts", added: 1, removed: 0 }]),
    );
    vi.mocked(useBranchConflicts).mockReturnValue(
      verdicts([{ ref: "main", clean: false, files: ["src/a.ts"] }], true),
    );
    render(
      <MemoryRouter>
        <WorktreesPanel
          conversationId="conv_root"
          sessions={[child({ id: "conv_wt", git_branch: "omni/login-a1b2c3" })]}
          orchestratorId="conv_root"
        />
      </MemoryRouter>,
    );

    const badge = screen.getByTestId("worktree-base-conflict");
    expect(badge.textContent).toBe("conflicts with main");
    expect(badge.getAttribute("title")).toContain("committed changes only");
    fireEvent.click(screen.getByRole("button", { name: /^Expand / }));
    expect(screen.getByTestId("worktree-conflicts").textContent).toContain("src/a.ts");
    fireEvent.click(screen.getByRole("button", { name: "Tell orchestrator" }));
    const [text] = vi.mocked(notify.mutate).mock.calls[0] as [string];
    expect(text).toContain("omni/login-a1b2c3");
    expect(text).toContain("the base main: src/a.ts");
    expect(vi.mocked(useNotifyOrchestrator)).toHaveBeenCalledWith("conv_root");
  });

  it("names the rival task in the orchestrator notice", () => {
    const text = conflictNotice(
      { title: "login", branch: "omni/login-1", sessionId: "conv_a" },
      "main",
      [{ ref: "omni/billing-2", clean: false, files: ["src/shared.ts", "src/b.ts"] }],
      [{ branch: "omni/billing-2", title: "billing" }],
    );
    expect(text).toContain('task "login" (omni/login-1, session conv_a)');
    expect(text).toContain("- with billing (omni/billing-2): src/shared.ts, src/b.ts");
    expect(text).toContain("rebase onto main");
  });
});

describe("findOverlaps", () => {
  it("maps each task's shared paths to the other tasks' titles", () => {
    const overlaps = findOverlaps([
      { id: "a", title: "login", paths: ["x.ts", "y.ts"] },
      { id: "b", title: "billing", paths: ["x.ts"] },
      { id: "c", title: "docs", paths: ["x.ts", "z.md"] },
    ]);
    expect(overlaps.get("a")).toEqual(new Map([["x.ts", ["billing", "docs"]]]));
    expect(overlaps.get("c")).toEqual(new Map([["x.ts", ["login", "billing"]]]));
    expect(findOverlaps([{ id: "a", title: "solo", paths: ["x.ts", "x.ts"] }]).size).toBe(0);
  });
});

describe("task board", () => {
  it("groups tasks by stage, most urgent first", () => {
    vi.mocked(useBranchChanges).mockImplementation((id: string | undefined) => {
      const files = id === "conv_idle" ? [] : [{ path: `${id}.ts`, added: 1, removed: 0 }];
      const result = branchResult(files);
      if (id === "conv_landed" && result.data) result.data.landed = true;
      return result;
    });
    render(
      <MemoryRouter>
        <WorktreesPanel
          conversationId="conv_root"
          sessions={[
            child({ id: "conv_landed", session_name: "landed", git_branch: "omni/l" }),
            child({ id: "conv_review", session_name: "review", git_branch: "omni/r" }),
            child({ id: "conv_run", session_name: "run", git_branch: "omni/x", busy: true }),
            child({
              id: "conv_ask",
              session_name: "ask",
              git_branch: "omni/a",
              pending_elicitations_count: 1,
            }),
            child({ id: "conv_idle", session_name: "idle", git_branch: "omni/i" }),
          ]}
        />
      </MemoryRouter>,
    );

    const order = [...document.querySelectorAll('[data-testid^="worktree-stage-"]')].map((el) =>
      el.getAttribute("data-testid"),
    );
    expect(order).toEqual([
      "worktree-stage-input",
      "worktree-stage-running",
      "worktree-stage-review",
      "worktree-stage-landed",
      "worktree-stage-idle",
    ]);
    const landed = screen.getByTestId("worktree-stage-landed");
    expect(landed.querySelector('[data-child-session-id="conv_landed"]')).not.toBeNull();
  });

  it("puts a waiting approval above a running turn", () => {
    expect(
      taskStage(child({ busy: true, pending_elicitations_count: 2 }), { landed: false, data: [] }),
    ).toBe("input");
    expect(taskStage(child({}), undefined)).toBe("idle");
  });
});
