// "Worktrees" view of the Agents rail: one row per sub-agent that runs in
// its own git worktree, with what its branch changed since it forked.
//
// Each row shows the task, `branch → base` and the total +/− of the branch
// (committed, uncommitted and untracked, via the git/changes endpoint). It
// expands into the changed files; opening one lands on the child session with
// the file in its branch diff (`?file=…&diff=1&diffsrc=branch`), where the
// regular comment layer annotates that worktree. Tasks sharing files get a
// `git merge-tree` dry-run that tells real conflicts from clean overlaps.

import { ChevronDownIcon, ChevronRightIcon, GitBranchIcon, TriangleAlertIcon } from "lucide-react";
import { useState } from "react";

import { RunningDot } from "@/components/RunningDot";
import {
  type BranchConflictVerdict,
  DIFF_SOURCE_PARAM,
  findOverlaps,
  useBranchChanges,
  useBranchConflicts,
  useBranchChangesForSessions,
} from "@/hooks/useBranchDiff";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import {
  LandError,
  type LandStrategy,
  useLandBranch,
  useNotifyOrchestrator,
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
  /** The orchestrator session, notified about conflicting tasks. */
  orchestratorId?: string;
}

/** Another task changing some of the same files. */
interface RivalTask {
  branch: string;
  title: string;
}

/** Message asking the orchestrator to sequence conflicting tasks. */
export function conflictNotice(
  task: { title: string; branch: string; sessionId: string },
  base: string | null,
  conflicts: BranchConflictVerdict[],
  rivals: RivalTask[],
): string {
  const titleOf = (ref: string) => rivals.find((rival) => rival.branch === ref)?.title ?? ref;
  const lines = conflicts.map(
    (verdict) =>
      `- with ${verdict.ref === base ? `the base ${verdict.ref}` : `${titleOf(verdict.ref)} (${verdict.ref})`}: ${verdict.files.join(", ")}`,
  );
  return [
    `Merge conflicts predicted for task "${task.title}" (${task.branch}, session ${task.sessionId}):`,
    ...lines,
    "",
    "Please coordinate: let one task finish and land first, then ask the other " +
      `(sys_session_send) to rebase onto ${base ?? "its base"} and resolve the conflicts.`,
  ].join("\n");
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
type TaskStage = "input" | "running" | "review" | "landed" | "idle";

/** Board sections, in display order. */
const STAGES: { stage: TaskStage; label: string }[] = [
  { stage: "input", label: "Needs input" },
  { stage: "running", label: "Running" },
  { stage: "review", label: "Ready for review" },
  { stage: "landed", label: "Landed" },
  { stage: "idle", label: "No changes yet" },
];

/**
 * Place a task on the board.
 *
 * @param child - The worktree sub-agent.
 * @param changes - Its branch changes, when loaded.
 * @returns The board section it belongs to.
 */
export function taskStage(
  child: ChildSessionInfo,
  changes: { landed: boolean; data: unknown[] } | undefined,
): TaskStage {
  if (child.pending_elicitations_count > 0) return "input";
  if (child.busy) return "running";
  if (changes?.landed) return "landed";
  if ((changes?.data.length ?? 0) > 0) return "review";
  return "idle";
}

/** Display title of a worktree task. */
function taskTitle(child: ChildSessionInfo): string {
  return child.task_summary || child.session_name || child.title || child.id;
}

export function WorktreesPanel({ conversationId, sessions, orchestratorId }: WorktreesPanelProps) {
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
  const pathsOf = new Map(
    worktrees.map((child, index) => [
      child.id,
      new Set(branchLists[index]?.data?.data.map((file) => file.path) ?? []),
    ]),
  );
  const rivalsOf = (child: ChildSessionInfo): RivalTask[] => {
    const mine = pathsOf.get(child.id) ?? new Set<string>();
    return worktrees
      .filter(
        (other) =>
          other.id !== child.id && [...(pathsOf.get(other.id) ?? [])].some((p) => mine.has(p)),
      )
      .map((other) => ({ branch: other.git_branch!, title: taskTitle(other) }));
  };
  if (worktrees.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center px-4 py-8 text-center text-sm text-muted-foreground">
        No sub-agent worktrees yet. Sub-agents dispatched with <code>worktree: true</code> appear
        here with their branch changes.
      </div>
    );
  }
  // Board: group tasks by where they stand, most urgent first.
  const stages = worktrees.map((child, index) => taskStage(child, branchLists[index]?.data));
  return (
    <div aria-label="Worktrees" className="flex min-h-0 flex-1 flex-col overflow-y-auto pb-1">
      {STAGES.map(({ stage, label }) => {
        const tasks = worktrees.filter((_, index) => stages[index] === stage);
        if (tasks.length === 0) return null;
        return (
          <section key={stage} aria-label={label} data-testid={`worktree-stage-${stage}`}>
            <h3 className="sticky top-0 z-10 bg-card px-2 pb-0.5 pt-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              {label} <span className="tabular-nums">{tasks.length}</span>
            </h3>
            <ul>
              {tasks.map((child) => (
                <WorktreeRow
                  key={child.id}
                  child={child}
                  isActive={child.id === conversationId}
                  overlaps={overlaps.get(child.id)}
                  rivals={rivalsOf(child)}
                  orchestratorId={orchestratorId}
                />
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

function WorktreeRow({
  child,
  isActive,
  overlaps,
  rivals,
  orchestratorId,
}: {
  child: ChildSessionInfo;
  isActive: boolean;
  /** Path -> other tasks changing it too; absent when nothing overlaps. */
  overlaps?: Map<string, string[]>;
  /** Tasks sharing files with this one, to dry-run merges against. */
  rivals: RivalTask[];
  orchestratorId?: string;
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
  const ports = changes.data?.ports ?? null;
  const prediction = useBranchConflicts(
    child.id,
    rivals.map((rival) => rival.branch),
    { enabled: files.length > 0 },
  );
  const conflicts = (prediction.data?.results ?? []).filter((verdict) => verdict.clean === false);
  const baseConflict = conflicts.find((verdict) => verdict.ref === base);
  const rivalConflicts = conflicts.filter((verdict) => verdict !== baseConflict);
  const overlapsClean =
    !!overlaps &&
    rivals.length > 0 &&
    rivals.every(
      (rival) =>
        prediction.data?.results.find((verdict) => verdict.ref === rival.branch)?.clean === true,
    );
  const committedOnly = prediction.data?.dirty ? " (committed changes only)" : "";

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
            {baseConflict && (
              <span
                data-testid="worktree-base-conflict"
                className="flex shrink-0 items-center gap-0.5 text-destructive"
                title={`Conflicts with ${base}${committedOnly}: ${baseConflict.files.join(", ")}`}
              >
                <TriangleAlertIcon aria-hidden="true" className="size-3" />
                conflicts with {base}
              </span>
            )}
            {overlaps && (
              <span
                data-testid="worktree-overlap"
                data-conflict={rivalConflicts.length > 0 ? "true" : undefined}
                className={cn(
                  "flex shrink-0 items-center gap-0.5",
                  rivalConflicts.length > 0
                    ? "text-destructive"
                    : overlapsClean
                      ? "text-muted-foreground"
                      : "text-warning",
                )}
                title={
                  rivalConflicts.length > 0
                    ? `Conflicts with ${rivalConflicts.map((verdict) => verdict.ref).join(", ")}${committedOnly}`
                    : overlapsClean
                      ? `Also changed by ${sharedWith}; predicted to merge cleanly${committedOnly}`
                      : `Also changed by ${sharedWith}; landing both may conflict`
                }
              >
                <TriangleAlertIcon aria-hidden="true" className="size-3" />
                {rivalConflicts.length > 0 ? "conflict" : `${overlaps.size} shared`}
              </span>
            )}
            {ports && (
              <span
                data-testid="worktree-ports"
                className="shrink-0 font-mono"
                title={`Dev server ports for this worktree: PORT=${ports.base}, OMNIGENT_PORT_BASE=${ports.base}, ${ports.span} ports`}
              >
                :{ports.base}–{ports.base + ports.span - 1}
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
          {conflicts.length > 0 && (
            <ConflictDetails
              conflicts={conflicts}
              notice={conflictNotice(
                { title, branch: child.git_branch ?? "", sessionId: child.id },
                base ?? null,
                conflicts,
                rivals,
              )}
              orchestratorId={orchestratorId}
              committedOnly={committedOnly !== ""}
            />
          )}
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

function ConflictDetails({
  conflicts,
  notice,
  orchestratorId,
  committedOnly,
}: {
  conflicts: BranchConflictVerdict[];
  notice: string;
  orchestratorId?: string;
  committedOnly: boolean;
}) {
  const notify = useNotifyOrchestrator(orchestratorId);
  return (
    <div data-testid="worktree-conflicts" className="mb-2 flex flex-col gap-1 text-xs">
      <p className="font-medium text-destructive">
        Predicted merge conflicts{committedOnly ? " (committed changes only)" : ""}:
      </p>
      <ul className="ml-3 list-disc">
        {conflicts.map((verdict) => (
          <li key={verdict.ref}>
            <span className="font-mono">{verdict.ref}</span>:{" "}
            <span className="font-mono">{verdict.files.join(", ")}</span>
          </li>
        ))}
      </ul>
      {orchestratorId && (
        <button
          type="button"
          disabled={notify.isPending || notify.isSuccess}
          title="Ask the orchestrator to sequence these tasks"
          onClick={() => notify.mutate(notice)}
          className="self-start rounded-full border border-border px-2 py-0.5 hover:bg-accent disabled:opacity-50"
        >
          {notify.isSuccess ? "Orchestrator notified" : "Tell orchestrator"}
        </button>
      )}
      {notify.isError && <p className="text-destructive">{notify.error.message}</p>}
    </div>
  );
}
