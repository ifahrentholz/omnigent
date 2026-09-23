// "Worktrees" view of the Agents rail: one row per sub-agent that runs in
// its own git worktree, with what its branch changed since it forked.
//
// Each row shows the task, `branch → base` and the total +/− of the branch
// (committed, uncommitted and untracked, via the git/changes endpoint). It
// expands into the changed files; opening one lands on the child session with
// the file in its branch diff (`?file=…&diff=1&diffsrc=branch`), where the
// regular comment layer annotates that worktree.

import { ChevronDownIcon, ChevronRightIcon, GitBranchIcon, TriangleAlertIcon } from "lucide-react";
import { useState } from "react";

import { RunningDot } from "@/components/RunningDot";
import {
  DIFF_SOURCE_PARAM,
  findOverlaps,
  useBranchChanges,
  useBranchChangesForSessions,
} from "@/hooks/useBranchDiff";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import {
  LandError,
  type LandStrategy,
  useLandBranch,
  useRequestPullRequest,
} from "@/hooks/useLandBranch";
import { Link, useLocation } from "@/lib/routing";
import { sessionNavigationSearch } from "@/lib/sessionNavigation";
import { cn } from "@/lib/utils";

interface WorktreesPanelProps {
  /** The conversation rendered in main, to highlight its row. */
  conversationId: string;
  /** Direct child sessions of the orchestrator. */
  sessions: ChildSessionInfo[];
}

/** Search string that opens `path` in a session's branch diff. */
function branchDiffSearch(search: string, path?: string): string {
  const params = new URLSearchParams(sessionNavigationSearch(search));
  params.set(DIFF_SOURCE_PARAM, "branch");
  if (path) {
    params.set("file", path);
    params.set("diff", "1");
  }
  return `?${params.toString()}`;
}

/**
 * List an orchestrator's worktree sub-agents with their branch changes.
 *
 * @param props See {@link WorktreesPanelProps}.
 * @returns The worktree list, or an explanatory empty state.
 */
/** Display title of a worktree task. */
function taskTitle(child: ChildSessionInfo): string {
  return child.task_summary || child.session_name || child.title || child.id;
}

export function WorktreesPanel({ conversationId, sessions }: WorktreesPanelProps) {
  const worktrees = sessions.filter((child) => child.git_branch);
  // Files touched by several parallel tasks will conflict when they land.
  const branchLists = useBranchChangesForSessions(worktrees.map((child) => child.id));
  const overlaps = findOverlaps(
    worktrees.map((child, index) => ({
      id: child.id,
      title: taskTitle(child),
      paths: branchLists[index]?.data?.data.map((file) => file.path) ?? [],
    })),
  );
  if (worktrees.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center px-4 py-8 text-center text-sm text-muted-foreground">
        No sub-agent worktrees yet. Sub-agents dispatched with <code>worktree: true</code> appear
        here with their branch changes.
      </div>
    );
  }
  return (
    <ul aria-label="Worktrees" className="flex min-h-0 flex-1 flex-col overflow-y-auto pb-1">
      {worktrees.map((child) => (
        <WorktreeRow
          key={child.id}
          child={child}
          isActive={child.id === conversationId}
          overlaps={overlaps.get(child.id)}
        />
      ))}
    </ul>
  );
}

function WorktreeRow({
  child,
  isActive,
  overlaps,
}: {
  child: ChildSessionInfo;
  isActive: boolean;
  /** Path -> other tasks changing it too; absent when nothing overlaps. */
  overlaps?: Map<string, string[]>;
}) {
  const [expanded, setExpanded] = useState(false);
  const location = useLocation();
  const changes = useBranchChanges(child.id);
  const files = changes.data?.data ?? [];
  const added = files.reduce((sum, file) => sum + (file.lines_added ?? 0), 0);
  const removed = files.reduce((sum, file) => sum + (file.lines_removed ?? 0), 0);
  const title = taskTitle(child);
  const sharedWith = overlaps ? [...new Set([...overlaps.values()].flat())].sort().join(", ") : "";
  const base = changes.data?.base ?? child.git_base_branch;
  const unavailable = changes.data && !changes.data.available ? changes.data.reason : null;

  return (
    <li
      data-testid="worktree-row"
      data-child-session-id={child.id}
      className={cn("border-b border-border/60", isActive && "bg-accent/60")}
    >
      <div className="flex items-start gap-1 px-2 py-2">
        <button
          type="button"
          aria-expanded={expanded}
          aria-label={expanded ? `Collapse ${title}` : `Expand ${title}`}
          onClick={() => setExpanded((open) => !open)}
          className="mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-sm text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          {expanded ? (
            <ChevronDownIcon className="size-3.5" />
          ) : (
            <ChevronRightIcon className="size-3.5" />
          )}
        </button>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <div className="flex min-w-0 items-center gap-1.5">
            {child.busy && <RunningDot />}
            <Link
              to={{ pathname: `/c/${child.id}`, search: branchDiffSearch(location.search) }}
              className="min-w-0 truncate text-sm font-medium hover:underline"
              title={child.title ?? undefined}
            >
              {title}
            </Link>
            <span className="ml-auto shrink-0 text-xs tabular-nums">
              {changes.isLoading ? (
                <span className="text-muted-foreground">…</span>
              ) : (
                <>
                  <span className="text-success">+{added}</span>{" "}
                  <span className="text-destructive">−{removed}</span>
                </>
              )}
            </span>
          </div>
          <div className="flex min-w-0 items-center gap-1 text-xs text-muted-foreground">
            <GitBranchIcon aria-hidden="true" className="size-3 shrink-0" />
            <span className="truncate font-mono">{child.git_branch}</span>
            {base && (
              <span className="shrink-0">
                → <span className="font-mono">{base}</span>
              </span>
            )}
            {overlaps && (
              <span
                data-testid="worktree-overlap"
                className="flex shrink-0 items-center gap-0.5 text-warning"
                title={`Also changed by ${sharedWith}; landing both may conflict`}
              >
                <TriangleAlertIcon aria-hidden="true" className="size-3" />
                {overlaps.size} shared
              </span>
            )}
            <span className="ml-auto shrink-0">
              {files.length} file{files.length === 1 ? "" : "s"}
            </span>
          </div>
        </div>
      </div>
      {expanded && (
        <div className="pb-2 pl-7 pr-2">
          <LandActions sessionId={child.id} base={base ?? null} disabled={child.busy} />
          {unavailable ? (
            <p className="text-xs text-muted-foreground">{unavailable}</p>
          ) : changes.isError ? (
            <p className="text-xs text-destructive">
              Failed to load: {changes.error instanceof Error ? changes.error.message : "error"}
            </p>
          ) : files.length === 0 && !changes.isLoading ? (
            <p className="text-xs text-muted-foreground">No changes on this branch yet.</p>
          ) : (
            <ul className="flex flex-col">
              {files.map((file) => (
                <li key={file.path}>
                  <Link
                    to={{
                      pathname: `/c/${child.id}`,
                      search: branchDiffSearch(location.search, file.path),
                    }}
                    data-testid="worktree-file"
                    className="flex min-w-0 items-center gap-2 rounded px-1 py-0.5 text-xs hover:bg-accent"
                    title={
                      overlaps?.has(file.path)
                        ? `Also changed by ${overlaps.get(file.path)?.join(", ")}`
                        : file.renamed
                          ? `${file.previous_path} → ${file.path}`
                          : file.path
                    }
                  >
                    {overlaps?.has(file.path) && (
                      <TriangleAlertIcon
                        aria-label="changed by another task too"
                        className="size-3 shrink-0 text-warning"
                      />
                    )}
                    <span
                      className={cn(
                        "min-w-0 flex-1 truncate font-mono",
                        file.status === "deleted" && "line-through text-muted-foreground",
                      )}
                    >
                      {file.path}
                    </span>
                    <span className="shrink-0 tabular-nums">
                      <span className="text-success">+{file.lines_added ?? 0}</span>{" "}
                      <span className="text-destructive">−{file.lines_removed ?? 0}</span>
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}

/**
 * "Land" controls for one task: merge its branch into the base, or ask the
 * worker to open a pull request instead.
 */
function LandActions({
  sessionId,
  base,
  disabled,
}: {
  sessionId: string;
  base: string | null;
  disabled: boolean;
}) {
  const [confirming, setConfirming] = useState(false);
  const [strategy, setStrategy] = useState<LandStrategy>("merge");
  const land = useLandBranch(sessionId);
  const requestPr = useRequestPullRequest(sessionId);
  const target = base ?? "base";

  return (
    <div className="mb-2 flex flex-col gap-1 text-xs">
      <div className="flex flex-wrap items-center gap-1">
        {confirming ? (
          <>
            <select
              aria-label="Merge strategy"
              value={strategy}
              onChange={(event) => setStrategy(event.target.value as LandStrategy)}
              className="rounded border border-border bg-transparent px-1 py-0.5"
            >
              <option value="merge">Merge commit</option>
              <option value="squash">Squash</option>
            </select>
            <button
              type="button"
              disabled={land.isPending}
              onClick={() => land.mutate({ strategy }, { onSettled: () => setConfirming(false) })}
              className="rounded-full border border-border px-2 py-0.5 font-medium hover:bg-accent disabled:opacity-50"
            >
              {land.isPending ? "Landing…" : `Land into ${target}`}
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className="rounded-full px-2 py-0.5 text-muted-foreground hover:text-foreground"
            >
              Cancel
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              disabled={disabled}
              title={
                disabled ? "Wait until the worker is idle" : `Merge this branch into ${target}`
              }
              onClick={() => {
                land.reset();
                setConfirming(true);
              }}
              className="rounded-full border border-border px-2 py-0.5 hover:bg-accent disabled:opacity-50"
            >
              Land…
            </button>
            <button
              type="button"
              disabled={requestPr.isPending || requestPr.isSuccess}
              title="Ask the worker to push its branch and open a pull request"
              onClick={() => requestPr.mutate()}
              className="rounded-full border border-border px-2 py-0.5 hover:bg-accent disabled:opacity-50"
            >
              {requestPr.isSuccess ? "PR requested" : "Ask for PR"}
            </button>
          </>
        )}
      </div>
      {land.isSuccess && (
        <p className="text-success">
          Landed into {land.data.base} ({land.data.commit.slice(0, 7)}).
        </p>
      )}
      {land.isError && (
        <div className="text-destructive">
          <p>{land.error.message}</p>
          {land.error instanceof LandError && land.error.conflicts.length > 0 && (
            <ul className="ml-3 list-disc font-mono">
              {land.error.conflicts.map((path) => (
                <li key={path}>{path}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
