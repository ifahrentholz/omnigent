// Mutations for landing a task worktree's branch:
//   POST /v1/sessions/{id}/resources/git/merge  (merge into the base, server-side)
//   POST /v1/sessions/{id}/events               (ask the worker to open a PR,
//                                                or the orchestrator to coordinate)

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { BRANCH_CHANGES_QUERY_PREFIX } from "@/hooks/useBranchDiff";
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
      // The base moved: every sibling's changes and conflict verdicts are stale.
      void queryClient.invalidateQueries({ queryKey: [BRANCH_CHANGES_QUERY_PREFIX] });
    },
  });
}

const OPEN_PR_REQUEST =
  "Your task is ready for review. Push your branch and open a pull request with " +
  "`gh pr create` (clear title, what changed, how you verified it). Do not merge it.";

/** Post a user message into a session. */
async function postUserMessage(sessionId: string, text: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      type: "message",
      data: { role: "user", content: [{ type: "input_text", text }] },
    }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
}

/**
 * Tell the orchestrator that parallel tasks will conflict so it can sequence them.
 *
 * @param orchestratorId - The session that dispatched the tasks.
 */
export function useNotifyOrchestrator(orchestratorId: string | undefined) {
  return useMutation({
    mutationFn: async (text: string) => {
      if (!orchestratorId) throw new Error("no orchestrator session");
      await postUserMessage(orchestratorId, text);
    },
  });
}

/**
 * Ask a worker session to push its branch and open a pull request.
 *
 * @param sessionId - The worktree sub-agent session.
 */
export function useRequestPullRequest(sessionId: string) {
  return useMutation({
    mutationFn: () => postUserMessage(sessionId, OPEN_PR_REQUEST),
  });
}
