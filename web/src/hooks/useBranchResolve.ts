// Manual conflict resolution in a task's worktree:
//   POST /v1/sessions/{id}/resources/git/resolve  {action, path?, content?, side?, message?}
// `status` reads the merge, `start` merges the base without committing,
// `resolve` fixes one file, `complete` commits and `abort` restores.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { BRANCH_CHANGES_QUERY_PREFIX } from "@/hooks/useBranchDiff";
import { authenticatedFetch } from "@/lib/identity";

/** One file git left unmerged; texts are null for binary files. */
export interface UnmergedFile {
  path: string;
  binary: boolean;
  /** The task branch's version. */
  ours: string | null;
  /** The base's version. */
  theirs: string | null;
  ancestor: string | null;
  /** The working file, with conflict markers. */
  working: string | null;
  deleted_in_ours: boolean;
  deleted_in_theirs: boolean;
}

export interface MergeState {
  in_progress: boolean;
  base: string;
  branch: string;
  conflicts: UnmergedFile[];
}

export interface MergeCompleted {
  committed: true;
  commit: string;
  branch: string;
  base: string;
  resolved: string[];
}

export type ResolveStep =
  | { action: "status" | "start" | "abort" }
  | { action: "resolve"; path: string; content: string }
  | { action: "resolve"; path: string; side: "ours" | "theirs" }
  | { action: "complete"; message?: string };

async function postResolve<T>(sessionId: string, step: ResolveStep): Promise<T> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/resources/git/resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(step),
    },
  );
  const body = (await res.json().catch(() => ({}))) as {
    error?: { message?: string };
    detail?: string;
  };
  if (!res.ok) {
    throw new Error(body.error?.message ?? body.detail ?? `${res.status} ${res.statusText}`);
  }
  return body as T;
}

function mergeStateKey(sessionId: string) {
  return [BRANCH_CHANGES_QUERY_PREFIX, sessionId, "resolve"] as const;
}

/**
 * The worktree's merge state, while the resolver is open.
 *
 * @param sessionId - The worktree sub-agent session.
 * @param enabled - Only fetch while the resolver is shown.
 */
export function useMergeState(sessionId: string, enabled: boolean) {
  return useQuery({
    queryKey: mergeStateKey(sessionId),
    queryFn: () => postResolve<MergeState>(sessionId, { action: "status" }),
    enabled,
    staleTime: 0,
  });
}

/**
 * Run one resolution step; merge-state results replace the cached state.
 *
 * @param sessionId - The worktree sub-agent session.
 */
export function useResolveStep(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (step: ResolveStep) => postResolve<MergeState | MergeCompleted>(sessionId, step),
    onSuccess: (result, step) => {
      if ("in_progress" in result) queryClient.setQueryData(mergeStateKey(sessionId), result);
      if (step.action === "complete" || step.action === "abort") {
        // The branch moved (or its merge was dropped): refresh changes and verdicts.
        void queryClient.invalidateQueries({ queryKey: [BRANCH_CHANGES_QUERY_PREFIX] });
      }
    },
  });
}
