// TanStack Query hooks for the task-branch diff endpoints:
//   GET /v1/sessions/{id}/resources/git/changes?base=
//   GET /v1/sessions/{id}/resources/git/diff/{path}?base=&previous_path=
//
// Unlike the environment `changes` list (working tree vs HEAD), these span
// everything the session's branch changed since it forked from its base:
// committed, uncommitted and untracked. A worker that commits in its
// worktree keeps its changes visible here.

import { useQueries, useQuery } from "@tanstack/react-query";
import { useSearchParams } from "@/lib/routing";
import {
  RunnerOfflineError,
  isRunnerUnavailable503,
  runnerOfflineRetryDelay,
  shouldRetryRunnerOffline,
  useSessionActive,
  useTrailingInvalidate,
  useWorkspaceServeable,
  type WorkspaceChangedFile,
} from "@/hooks/useWorkspaceChangedFiles";
import { authenticatedFetch } from "@/lib/identity";

/** Which baseline the Changes list and diff view compare against. */
export type DiffSource = "head" | "branch";

/** URL param selecting the branch baseline; shareable like `?diff=1`. */
export const DIFF_SOURCE_PARAM = "diffsrc";

export const BRANCH_CHANGES_QUERY_PREFIX = "workspace-branch-changes";

/** A branch change in the Changes-list shape, plus rename information. */
export interface BranchChangedFile extends WorkspaceChangedFile {
  /** Old path when the branch renamed the file, else null. */
  previous_path: string | null;
  /** True for renames (listed with status "modified"). */
  renamed: boolean;
}

export interface BranchChangesResult {
  /** False when the session has no git workspace to diff (400/404). */
  available: boolean;
  /** Why the branch view is unavailable, e.g. "workspace is not a git repository". */
  reason: string | null;
  /** Base branch the list compares against, e.g. "main". */
  base: string | null;
  /** Commit the branch forked from. */
  mergeBase: string | null;
  data: BranchChangedFile[];
}

export interface BranchFileDiff {
  path: string;
  before: string | null;
  after: string | null;
}

interface BranchChangesWire {
  base?: string;
  merge_base?: string;
  data: {
    path: string;
    name: string;
    status: "created" | "modified" | "deleted" | "renamed";
    previous_path?: string | null;
    bytes: number | null;
    modified_at: number | null;
    lines_added: number | null;
    lines_removed: number | null;
  }[];
}

/** Read the server's error message, falling back to the status line. */
async function errorMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { error?: { message?: string }; detail?: string };
    return body?.error?.message ?? body?.detail ?? `${res.status} ${res.statusText}`;
  } catch {
    return `${res.status} ${res.statusText}`;
  }
}

function sessionUrl(sessionId: string, suffix: string, params: Record<string, string | null>) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value) query.set(key, value);
  const qs = query.toString();
  return `/v1/sessions/${encodeURIComponent(sessionId)}/resources/git/${suffix}${qs ? `?${qs}` : ""}`;
}

/** Fetch every file a session's branch changed since its base. */
export async function fetchBranchChanges(
  sessionId: string,
  base?: string | null,
): Promise<BranchChangesResult> {
  const res = await authenticatedFetch(sessionUrl(sessionId, "changes", { base: base ?? null }));
  if (res.status === 400 || res.status === 404) {
    return {
      available: false,
      reason: await errorMessage(res),
      base: null,
      mergeBase: null,
      data: [],
    };
  }
  if (res.status === 503 && (await isRunnerUnavailable503(res))) throw new RunnerOfflineError();
  if (!res.ok) throw new Error(await errorMessage(res));
  const json = (await res.json()) as BranchChangesWire;
  return {
    available: true,
    reason: null,
    base: json.base ?? null,
    mergeBase: json.merge_base ?? null,
    data: json.data.map((entry) => ({
      path: entry.path,
      name: entry.name,
      status: entry.status === "renamed" ? "modified" : entry.status,
      renamed: entry.status === "renamed",
      previous_path: entry.previous_path ?? null,
      bytes: entry.bytes,
      modified_at: entry.modified_at,
      lines_added: entry.lines_added ?? null,
      lines_removed: entry.lines_removed ?? null,
    })),
  };
}

async function fetchBranchFileDiff(
  sessionId: string,
  path: string,
  previousPath: string | null,
): Promise<BranchFileDiff> {
  const encodedPath = path.split("/").map(encodeURIComponent).join("/");
  const res = await authenticatedFetch(
    sessionUrl(sessionId, `diff/${encodedPath}`, { previous_path: previousPath }),
  );
  if (!res.ok) throw new Error(await errorMessage(res));
  const json = (await res.json()) as BranchFileDiff;
  return { path: json.path, before: json.before, after: json.after };
}

/** Query key of a session's branch-changes list. */
export function branchChangesQueryKey(sessionId: string | undefined) {
  return [BRANCH_CHANGES_QUERY_PREFIX, sessionId] as const;
}

/**
 * Files a session's branch changed since its base (defaults server-side to
 * the session's `git_base_branch`).
 *
 * @param sessionId - Session whose workspace to diff; may be a sub-agent child.
 * @param options.enabled - Gate the request, e.g. only while the branch view is shown.
 */
export function useBranchChanges(
  sessionId: string | undefined,
  options: { enabled?: boolean } = {},
) {
  const enabled = options.enabled ?? true;
  const serveable = useWorkspaceServeable(sessionId);
  const sessionActive = useSessionActive(sessionId);
  useTrailingInvalidate(sessionId, sessionActive, BRANCH_CHANGES_QUERY_PREFIX);
  return useQuery({
    queryKey: branchChangesQueryKey(sessionId),
    queryFn: () => fetchBranchChanges(sessionId!),
    enabled: enabled && !!sessionId && serveable !== false,
    retry: (failureCount, error) => shouldRetryRunnerOffline(failureCount, error),
    retryDelay: runnerOfflineRetryDelay,
    staleTime: 5_000,
  });
}

/**
 * Before/after content of one file across the whole branch.
 *
 * @param sessionId - Session whose workspace to read.
 * @param path - File path, or null to disable the query.
 * @param previousPath - Old path of a renamed file.
 */
export function useBranchFileDiff(
  sessionId: string | undefined,
  path: string | null,
  previousPath: string | null = null,
) {
  const serveable = useWorkspaceServeable(sessionId);
  return useQuery({
    queryKey: ["branch-file-diff", sessionId, path, previousPath],
    queryFn: () => fetchBranchFileDiff(sessionId!, path!, previousPath),
    enabled: !!sessionId && !!path && serveable !== false,
    staleTime: 5_000,
  });
}

/** The diff baseline selected by the URL (`?diffsrc=branch`), plus a setter. */
export function useDiffSource(): [DiffSource, (source: DiffSource) => void] {
  const [searchParams, setSearchParams] = useSearchParams();
  const source: DiffSource = searchParams.get(DIFF_SOURCE_PARAM) === "branch" ? "branch" : "head";
  const setSource = (next: DiffSource) =>
    setSearchParams(
      (prev) => {
        const params = new URLSearchParams(prev);
        if (next === "branch") params.set(DIFF_SOURCE_PARAM, "branch");
        else params.delete(DIFF_SOURCE_PARAM);
        return params;
      },
      { replace: true },
    );
  return [source, setSource];
}

/**
 * Branch changes of several sessions at once (shares the per-session cache
 * with {@link useBranchChanges}, so rows and the panel fetch each list once).
 *
 * @param sessionIds - Sessions to diff, e.g. an orchestrator's worktree children.
 * @returns One query result per id, in order.
 */
export function useBranchChangesForSessions(sessionIds: string[]) {
  return useQueries({
    queries: sessionIds.map((sessionId) => ({
      queryKey: branchChangesQueryKey(sessionId),
      queryFn: () => fetchBranchChanges(sessionId),
      staleTime: 5_000,
    })),
  });
}

/** One task's changed paths, for overlap detection. */
export interface TaskPaths {
  id: string;
  title: string;
  paths: string[];
}

/**
 * Find files changed by more than one parallel task.
 *
 * @param tasks - Each task's id, display title and changed paths.
 * @returns For every task id with overlaps: path -> titles of the *other*
 *   tasks that also change it. Tasks without overlaps are absent.
 */
export function findOverlaps(tasks: TaskPaths[]): Map<string, Map<string, string[]>> {
  const owners = new Map<string, TaskPaths[]>();
  for (const task of tasks) {
    for (const path of new Set(task.paths)) {
      owners.set(path, [...(owners.get(path) ?? []), task]);
    }
  }
  const overlaps = new Map<string, Map<string, string[]>>();
  for (const [path, sharing] of owners) {
    if (sharing.length < 2) continue;
    for (const task of sharing) {
      const others = sharing.filter((other) => other.id !== task.id).map((other) => other.title);
      const perTask = overlaps.get(task.id) ?? new Map<string, string[]>();
      perTask.set(path, others);
      overlaps.set(task.id, perTask);
    }
  }
  return overlaps;
}
