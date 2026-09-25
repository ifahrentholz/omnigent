// Resolve a task's conflicts with its base by hand, in the task's worktree.
//
// Start merges the base into the task branch without committing. Each
// conflicted file then shows its conflict hunks with the task's and the
// base's lines side by side; a choice rewrites that hunk in the editable
// result, which is saved (and staged) with "Mark resolved". Complete commits
// the merge, Abort restores the worktree.

import { CheckIcon, FileWarningIcon } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  type MergeCompleted,
  type UnmergedFile,
  useMergeState,
  useResolveStep,
} from "@/hooks/useBranchResolve";
import { manualResolutionNotice, useMessageWorker } from "@/hooks/useLandBranch";
import {
  type HunkChoice,
  countConflicts,
  parseConflicts,
  resolveHunk,
} from "@/lib/conflictMarkers";
import { cn } from "@/lib/utils";

interface ConflictResolverDialogProps {
  sessionId: string;
  /** The task branch, e.g. "omni/login-1". */
  branch: string;
  /** The base being merged in, e.g. "main". */
  base: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const BUTTON =
  "rounded-full border border-border px-2 py-0.5 text-xs hover:bg-accent disabled:opacity-50";

export function ConflictResolverDialog({
  sessionId,
  branch,
  base,
  open,
  onOpenChange,
}: ConflictResolverDialogProps) {
  const state = useMergeState(sessionId, open);
  const step = useResolveStep(sessionId);
  const tellWorker = useMessageWorker(sessionId);
  const [selected, setSelected] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [resolved, setResolved] = useState<string[]>([]);
  const [notify, setNotify] = useState(true);

  const merge = state.data;
  const conflicts = merge?.conflicts ?? [];
  const current = conflicts.find((file) => file.path === selected) ?? conflicts[0] ?? null;
  useEffect(() => {
    if (!open) {
      setDrafts({});
      setResolved([]);
      setSelected(null);
    }
  }, [open]);

  const run = (action: Parameters<typeof step.mutate>[0], onDone?: (r: unknown) => void) =>
    step.mutate(action, { onSuccess: onDone });

  const complete = () =>
    run({ action: "complete" }, (result) => {
      const done = result as MergeCompleted;
      if (notify) tellWorker.mutate(manualResolutionNotice(base, done.commit, done.resolved));
      onOpenChange(false);
    });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="conflict-resolver"
        className="flex max-h-[85vh] flex-col gap-3 sm:max-w-5xl"
      >
        <DialogHeader>
          <DialogTitle>
            Resolve <span className="font-mono">{branch}</span> against{" "}
            <span className="font-mono">{base}</span>
          </DialogTitle>
        </DialogHeader>

        {state.isLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : state.isError ? (
          <p className="text-sm text-destructive">{state.error.message}</p>
        ) : !merge?.in_progress ? (
          <div className="flex flex-col items-start gap-2 text-sm">
            <p className="text-muted-foreground">
              Merges <span className="font-mono">{base}</span> into the task branch in the
              worker&apos;s worktree without committing, so you can resolve each conflict here. The
              worker should stay idle meanwhile.
            </p>
            <button
              type="button"
              disabled={step.isPending}
              onClick={() => run({ action: "start" })}
              className={cn(BUTTON, "font-medium")}
            >
              {step.isPending ? "Merging…" : `Start merge of ${base}`}
            </button>
          </div>
        ) : (
          <div className="flex min-h-0 flex-1 gap-3">
            <ul aria-label="Conflicted files" className="w-56 shrink-0 overflow-y-auto text-xs">
              {conflicts.map((file) => (
                <li key={file.path}>
                  <button
                    type="button"
                    onClick={() => setSelected(file.path)}
                    className={cn(
                      "flex w-full items-center gap-1 rounded px-1.5 py-1 text-left font-mono hover:bg-accent",
                      file.path === current?.path && "bg-accent",
                    )}
                  >
                    <FileWarningIcon
                      aria-hidden="true"
                      className="size-3 shrink-0 text-destructive"
                    />
                    <span className="truncate">{file.path}</span>
                  </button>
                </li>
              ))}
              {resolved.map((path) => (
                <li
                  key={path}
                  data-testid="resolved-file"
                  className="flex items-center gap-1 px-1.5 py-1 font-mono text-muted-foreground"
                >
                  <CheckIcon aria-hidden="true" className="size-3 shrink-0 text-success" />
                  <span className="truncate">{path}</span>
                </li>
              ))}
              {conflicts.length === 0 && (
                <li className="px-1.5 py-1 text-muted-foreground">No conflicts left.</li>
              )}
            </ul>
            <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2 overflow-y-auto">
              {current && (
                <FileResolver
                  key={current.path}
                  file={current}
                  branch={branch}
                  base={base}
                  draft={drafts[current.path] ?? current.working ?? ""}
                  onDraftChange={(text) => setDrafts((d) => ({ ...d, [current.path]: text }))}
                  pending={step.isPending}
                  onResolve={(resolution) =>
                    run({ action: "resolve", path: current.path, ...resolution }, () =>
                      setResolved((r) => [...r, current.path]),
                    )
                  }
                />
              )}
            </div>
          </div>
        )}

        {step.isError && <p className="text-sm text-destructive">{step.error.message}</p>}

        {merge?.in_progress && (
          <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3 text-xs">
            <button
              type="button"
              disabled={step.isPending}
              onClick={() => run({ action: "abort" })}
              className={BUTTON}
            >
              Abort merge
            </button>
            <label className="ml-auto flex items-center gap-1 text-muted-foreground">
              <input
                type="checkbox"
                checked={notify}
                onChange={(event) => setNotify(event.target.checked)}
              />
              Tell the worker what was resolved
            </label>
            <button
              type="button"
              disabled={step.isPending || conflicts.length > 0}
              title={conflicts.length > 0 ? "Resolve every file first" : undefined}
              onClick={complete}
              className={cn(BUTTON, "font-medium")}
            >
              Complete merge
            </button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function FileResolver({
  file,
  branch,
  base,
  draft,
  onDraftChange,
  pending,
  onResolve,
}: {
  file: UnmergedFile;
  branch: string;
  base: string;
  draft: string;
  onDraftChange: (text: string) => void;
  pending: boolean;
  onResolve: (resolution: { content: string } | { side: "ours" | "theirs" }) => void;
}) {
  // Keyed by content (plus a counter for identical hunks) so a resolved hunk
  // drops out without the others remounting under shifted keys.
  const hunks = useMemo(() => {
    const seen = new Map<string, number>();
    return parseConflicts(draft).flatMap((segment) => {
      if (segment.kind !== "conflict") return [];
      const n = seen.get(segment.raw) ?? 0;
      seen.set(segment.raw, n + 1);
      return [{ hunk: segment, key: `${n}:${segment.raw}` }];
    });
  }, [draft]);
  const left = countConflicts(draft);

  if (file.binary || file.working === null) {
    const what = file.binary ? "Binary file" : "Deleted on one side";
    return (
      <div className="flex flex-col items-start gap-2 text-sm">
        <p className="text-muted-foreground">
          {what}: keep one version of <span className="font-mono">{file.path}</span> whole.
        </p>
        <div className="flex gap-1">
          <button
            type="button"
            disabled={pending}
            className={BUTTON}
            onClick={() => onResolve({ side: "ours" })}
          >
            {file.deleted_in_ours ? "Keep it deleted (task)" : "Keep task version"}
          </button>
          <button
            type="button"
            disabled={pending}
            className={BUTTON}
            onClick={() => onResolve({ side: "theirs" })}
          >
            {file.deleted_in_theirs ? `Keep it deleted (${base})` : `Keep ${base} version`}
          </button>
        </div>
      </div>
    );
  }

  const choose = (index: number, choice: HunkChoice) =>
    onDraftChange(resolveHunk(draft, index, choice));

  return (
    <div className="flex flex-col gap-2 text-xs">
      {hunks.map(({ hunk, key }, index) => (
        <section key={key} data-testid="conflict-hunk" className="rounded border border-border">
          <div className="flex items-center gap-1 border-b border-border px-2 py-1">
            <span className="font-medium">
              Conflict {index + 1} of {hunks.length}
            </span>
            <span className="ml-auto flex gap-1">
              <button type="button" className={BUTTON} onClick={() => choose(index, "ours")}>
                Keep task
              </button>
              <button type="button" className={BUTTON} onClick={() => choose(index, "theirs")}>
                Keep {base}
              </button>
              <button type="button" className={BUTTON} onClick={() => choose(index, "both")}>
                Keep both
              </button>
            </span>
          </div>
          <div className="grid grid-cols-2 divide-x divide-border">
            <HunkSide label={`Task (${branch})`} text={hunk.ours} />
            <HunkSide label={`Base (${base})`} text={hunk.theirs} />
          </div>
          {hunk.ancestor !== null && (
            <details className="border-t border-border px-2 py-1">
              <summary className="cursor-pointer text-muted-foreground">Common ancestor</summary>
              <pre className="whitespace-pre-wrap font-mono">{hunk.ancestor || "(empty)"}</pre>
            </details>
          )}
        </section>
      ))}
      <label className="flex flex-col gap-1">
        <span className="font-medium">
          Result for <span className="font-mono">{file.path}</span>
          {left > 0 && <span className="text-muted-foreground"> · {left} conflict(s) left</span>}
        </span>
        <textarea
          aria-label={`Result for ${file.path}`}
          value={draft}
          onChange={(event) => onDraftChange(event.target.value)}
          rows={12}
          spellCheck={false}
          className="rounded border border-border bg-transparent p-2 font-mono"
        />
      </label>
      <button
        type="button"
        disabled={pending || left > 0}
        title={left > 0 ? "Resolve every conflict in the result first" : undefined}
        onClick={() => onResolve({ content: draft })}
        className={cn(BUTTON, "self-start font-medium")}
      >
        Mark resolved
      </button>
    </div>
  );
}

function HunkSide({ label, text }: { label: string; text: string }) {
  return (
    <div className="min-w-0">
      <div className="px-2 pt-1 text-muted-foreground">{label}</div>
      <pre className="overflow-x-auto whitespace-pre-wrap px-2 pb-1 font-mono">
        {text || "(nothing)"}
      </pre>
    </div>
  );
}
