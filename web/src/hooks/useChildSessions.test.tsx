import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { fetchChildSessions, useChildSessions } from "./useChildSessions";

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
}));

function wrapper({ children }: PropsWithChildren) {
  return (
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      {children}
    </QueryClientProvider>
  );
}

describe("useChildSessions", () => {
  it("does not fetch child sessions for a provisional conversation", () => {
    const { result } = renderHook(() => useChildSessions("temp:pending-create"), { wrapper });

    expect(result.current.children).toEqual([]);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("maps a child's worktree fields and defaults them to null", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          data: [
            {
              id: "conv_wt",
              title: "worker:login",
              tool: "worker",
              session_name: "login",
              current_task_status: null,
              busy: true,
              workspace: "/r/repo-worktrees/omni-login-a1b2c3",
              git_branch: "omni/login-a1b2c3",
              git_base_branch: "main",
            },
            {
              id: "conv_shared",
              title: "worker:docs",
              tool: "worker",
              session_name: "docs",
              current_task_status: null,
              busy: false,
            },
          ],
        }),
        { status: 200 },
      ),
    );

    const [isolated, shared] = await fetchChildSessions("conv_parent");

    expect(isolated).toMatchObject({
      workspace: "/r/repo-worktrees/omni-login-a1b2c3",
      git_branch: "omni/login-a1b2c3",
      git_base_branch: "main",
    });
    expect(shared).toMatchObject({ workspace: null, git_branch: null, git_base_branch: null });
  });
});
