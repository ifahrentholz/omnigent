import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useBranchChanges } from "@/hooks/useBranchDiff";
import type * as BranchDiffModule from "@/hooks/useBranchDiff";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import { WorktreesPanel } from "./WorktreesPanel";

vi.mock("@/hooks/useBranchDiff", async (importOriginal) => ({
  ...(await importOriginal<typeof BranchDiffModule>()),
  useBranchChanges: vi.fn(),
}));

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

function branchResult(files: { path: string; added: number; removed: number }[]) {
  return {
    data: {
      available: true,
      reason: null,
      base: "main",
      mergeBase: "abc",
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
});
