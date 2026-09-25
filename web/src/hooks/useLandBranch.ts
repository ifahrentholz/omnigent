// Mutations for landing a task worktree's branch:
//   POST /v1/sessions/{id}/resources/git/merge  (merge into the base, server-side)
//   POST /v1/sessions/{id}/events               (ask the worker to open a PR or to
//                                                update from its base, or the
//                                                orchestrator to coordinate)

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

/**
 * Request asking a worker to merge its base branch and resolve the conflicts.
 *
 * @param base - The branch the task lands into, e.g. "main".
 * @param files - Paths predicted to conflict; may be empty.
 * @param note - The reviewer's guidance on which side wins, if any.
 */
export function updateFromBaseRequest(base: string, files: string[], note?: string): string {
  const where = files.length > 0 ? ` in: ${files.join(", ")}` : "";
  const lines = [
    `\`${base}\` has changes that conflict with your branch${where}.`,
    `Merge the local \`${base}\` branch into your branch (\`git merge ${base}\`; do not fetch ` +
      "or rebase), resolve every conflict so your task still does what it should on top of " +
      "what landed, run the relevant tests and commit the merge.",
    "If the two changes contradict each other and nothing here settles which one wins, " +
      "abort the merge (`git merge --abort`) and ask instead of guessing.",
  ];
  const guidance = note?.trim();
  if (guidance) lines.push("", `Reviewer's note: ${guidance}`);
  return lines.join("\n");
}

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

/** One worker to bring up to date with its base. */
export interface UpdateFromBase {
  sessionId: string;
  base: string;
  files: string[];
  note?: string;
}

/** Ask worker sessions to merge their base and resolve the conflicts. */
export function useRequestUpdateFromBase() {
  return useMutation({
    mutationFn: (requests: UpdateFromBase[]) =>
      Promise.all(
        requests.map((request) =>
          postUserMessage(
            request.sessionId,
            updateFromBaseRequest(request.base, request.files, request.note),
          ),
        ),
      ).then(() => undefined),
  });
}

/**
 * Message telling a worker its conflicts were resolved by hand.
 *
 * @param base - The base branch that was merged in, e.g. "main".
 * @param commit - The merge commit.
 * @param files - Files the merge touched.
 */
export function manualResolutionNotice(base: string, commit: string, files: string[]): string {
  const touched = files.length > 0 ? `; files: ${files.join(", ")}` : "";
  return [
    `I merged \`${base}\` into your branch and resolved the conflicts by hand ` +
      `(commit ${commit.slice(0, 7)}${touched}).`,
    "Keep that resolution: build on top of it, and rerun the relevant tests before you continue.",
  ].join("\n");
}

/**
 * Post a message to a worker session.
 *
 * @param sessionId - The worktree sub-agent session.
 */
export function useMessageWorker(sessionId: string) {
  return useMutation({
    mutationFn: (text: string) => postUserMessage(sessionId, text),
  });
}
