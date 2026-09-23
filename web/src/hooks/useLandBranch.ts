// Mutations for landing a task worktree's branch:
//   POST /v1/sessions/{id}/resources/git/merge  (merge into the base, server-side)
//   POST /v1/sessions/{id}/events               (ask the worker to open a PR)

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { branchChangesQueryKey } from "@/hooks/useBranchDiff";
import { authenticatedFetch } from "@/lib/identity";

export type LandStrategy = "merge" | "squash";

export interface LandResult {
  merged: true;
  branch: string;
  base: string;
  commit: string;
}

/** A refused or conflicting merge, with the conflicting paths when known. */
export class LandError extends Error {
  readonly conflicts: string[];

  constructor(message: string, conflicts: string[] = []) {
    super(message);
    this.name = "LandError";
    this.conflicts = conflicts;
  }
}

/**
 * Merge a task session's branch into its base branch.
 *
 * @param sessionId - The worktree sub-agent session.
 */
export function useLandBranch(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: { strategy: LandStrategy; message?: string }) => {
      const res = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/resources/git/merge`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      const body = (await res.json().catch(() => ({}))) as {
        error?: { message?: string; conflicts?: string[] };
        detail?: string;
      } & Partial<LandResult>;
      if (!res.ok) {
        throw new LandError(
          body.error?.message ?? body.detail ?? `${res.status} ${res.statusText}`,
          body.error?.conflicts ?? [],
        );
      }
      return body as LandResult;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: branchChangesQueryKey(sessionId) });
    },
  });
}

const OPEN_PR_REQUEST =
  "Your task is ready for review. Push your branch and open a pull request with " +
  "`gh pr create` (clear title, what changed, how you verified it). Do not merge it.";

/**
 * Ask a worker session to push its branch and open a pull request.
 *
 * @param sessionId - The worktree sub-agent session.
 */
export function useRequestPullRequest(sessionId: string) {
  return useMutation({
    mutationFn: async () => {
      const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/events`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          type: "message",
          data: { role: "user", content: [{ type: "input_text", text: OPEN_PR_REQUEST }] },
        }),
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    },
  });
}
