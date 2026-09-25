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
  /** True once every change of the branch is on its base (merged or squashed). */
  landed: boolean;
  /** The worktree's port range for dev servers, when one is allocated. */
  ports: WorktreePorts | null;
  /** The worktree's background setup (`setup_async`), when it has one. */
  setup: WorktreeSetupStatus | null;
  data: BranchChangedFile[];
}

export interface WorktreeSetupStatus {
  state: "running" | "ok" | "failed";
  /** The `setup_async` command. */
  command: string;
  exit_code: number | null;
  /** Why it failed, ending with the command's output. */
  error: string | null;
  /** Host path of the setup log. */
  log: string | null;
}

export interface WorktreePorts {
  /** Index unique among the repo's live worktrees, starting at 1. */
  index: number;
  /** First port of the range; exported as PORT. */
  base: number;
  /** Number of ports in the range. */
  span: number;
}

export interface BranchFileDiff {
  path: string;
  before: string | null;
  after: string | null;
}

interface BranchChangesWire {
  base?: string;
  merge_base?: string;
  landed?: boolean;
  ports?: WorktreePorts | null;
  setup?: WorktreeSetupStatus | null;
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

/** Poll a worktree while its background setup runs; nothing else signals its end. */
function pollWhileSettingUp(query: { state: { data?: BranchChangesResult } }): number | false {
  return query.state.data?.setup?.state === "running" ? SETUP_POLL_MS : false;
}

const SETUP_POLL_MS = 3_000;

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
      landed: false,
      ports: null,
      setup: null,
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
    landed: json.landed ?? false,
    ports: json.ports ?? null,
    setup: json.setup ?? null,
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
    refetchInterval: pollWhileSettingUp,
  });
}

/** Predicted outcome of merging the branch with one other branch. */
export interface BranchConflictVerdict {
  /** The compared branch, e.g. "main" or a sibling task's branch. */
  ref: string;
  /** True when the merge is clean; null when the branch could not be resolved. */
  clean: boolean | null;
  /** Paths that would conflict. */
  files: string[];
}

export interface BranchConflictsResult {
  /** Base branch the first verdict compares against. */
  base: string | null;
  /** Uncommitted edits exist; the prediction covers commits only. */
  dirty: boolean;
  /** False when the host's git is too old for `merge-tree --write-tree`. */
  supported: boolean;
  /** One verdict per compared branch, the base first. */
  results: BranchConflictVerdict[];
}

/**
 * Dry-run merges of a session's branch with its base and other branches.
 *
 * @returns The verdicts, or null when the session has no git workspace.
 */
export async function fetchBranchConflicts(
  sessionId: string,
  against: string[],
): Promise<BranchConflictsResult | null> {
  const res = await authenticatedFetch(
    sessionUrl(sessionId, "conflicts", { against: against.length ? against.join(",") : null }),
  );
  if (res.status === 400 || res.status === 404) return null;
  if (res.status === 503 && (await isRunnerUnavailable503(res))) throw new RunnerOfflineError();
  if (!res.ok) throw new Error(await errorMessage(res));
  const json = (await res.json()) as Partial<BranchConflictsResult>;
  return {
    base: json.base ?? null,
    dirty: json.dirty ?? false,
    supported: json.supported ?? true,
    results: json.results ?? [],
  };
}

/**
 * Predicted merge conflicts of a task branch with its base and sibling branches.
 *
 * Shares the branch-changes key prefix, so landing any task refreshes it.
 *
 * @param sessionId - The worktree sub-agent session.
 * @param against - Other task branches to compare with, e.g. overlapping siblings.
 */
export function useBranchConflicts(
  sessionId: string | undefined,
  against: string[],
  options: { enabled?: boolean } = {},
) {
  const enabled = options.enabled ?? true;
  const serveable = useWorkspaceServeable(sessionId);
  const key = [...against].sort().join(",");
  return useQuery({
    queryKey: [BRANCH_CHANGES_QUERY_PREFIX, sessionId, "conflicts", key] as const,
    queryFn: () => fetchBranchConflicts(sessionId!, key ? key.split(",") : []),
    enabled: enabled && !!sessionId && serveable !== false,
    retry: (failureCount, error) => shouldRetryRunnerOffline(failureCount, error),
    retryDelay: runnerOfflineRetryDelay,
    staleTime: 10_000,
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
      refetchInterval: pollWhileSettingUp,
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
