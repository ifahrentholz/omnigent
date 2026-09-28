// Resolve a task's conflicts with its base by hand, in the task's worktree.
//
// Start merges the base into the task branch without committing. Each
// conflicted file then opens in a Monaco merge editor (task vs base on top,
// the editable result below). The footer always offers the one next step:
// the next unresolved file, or "Commit merge", which saves every resolved
// file and commits. Abort restores the worktree.

import { CheckCircle2Icon, CircleAlertIcon, GitMergeIcon } from "lucide-react";
import { Suspense, lazy, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  type MergeCompleted,
  type UnmergedFile,
  useMergeState,
  useResolveStep,
} from "@/hooks/useBranchResolve";
import { manualResolutionNotice, useMessageWorker } from "@/hooks/useLandBranch";
import { countConflicts } from "@/lib/conflictMarkers";
import { cn } from "@/lib/utils";

const MergeEditor = lazy(() => import("./MergeEditor"));

interface ConflictResolverDialogProps {
  sessionId: string;
  /** The task branch, e.g. "omni/login-1". */
  branch: string;
  /** The base being merged in, e.g. "main". */
  base: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/** A file the editor can show; binary and delete/modify ones need a whole-file pick. */
function isText(file: UnmergedFile): boolean {
  return !file.binary && file.working !== null;
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

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
  const [picked, setPicked] = useState<string[]>([]);
  const [notify, setNotify] = useState(true);
  // The files being committed; saving each drops it from the server's list,
  // which shouldn't reshuffle the dialog mid-commit.
  const [committing, setCommitting] = useState<UnmergedFile[] | null>(null);

  const merge = state.data;
  const conflicts = committing ?? merge?.conflicts ?? [];
  const current = conflicts.find((file) => file.path === selected) ?? conflicts[0] ?? null;
  useEffect(() => {
    if (!open) {
      setDrafts({});
      setPicked([]);
      setSelected(null);
    }
  }, [open]);

  const draftOf = (file: UnmergedFile) => drafts[file.path] ?? file.working ?? "";
  // Conflicts left per file; binary files stay open until a side is picked.
  const leftIn = (file: UnmergedFile) => (isText(file) ? countConflicts(draftOf(file)) : 1);
  const openFiles = conflicts.filter((file) => leftIn(file) > 0);
  const nextFile = openFiles.find((file) => file.path !== current?.path) ?? null;
  const currentLeft = current ? leftIn(current) : 0;
  const ready = openFiles.length === 0;
  const busy = step.isPending || committing !== null;

  const run = (action: Parameters<typeof step.mutate>[0], onDone?: (r: unknown) => void) =>
    step.mutate(action, { onSuccess: onDone });

  // Save each resolved text file, then commit, so finishing is one click.
  const commit = async () => {
    setCommitting(conflicts);
    try {
      for (const file of conflicts.filter(isText)) {
        // One at a time: every resolve stages into the same git index.
        // eslint-disable-next-line no-await-in-loop
        await step.mutateAsync({ action: "resolve", path: file.path, content: draftOf(file) });
      }
      const done = (await step.mutateAsync({ action: "complete" })) as MergeCompleted;
      if (notify) tellWorker.mutate(manualResolutionNotice(base, done.commit, done.resolved));
      onOpenChange(false);
    } catch {
      // The mutation's error is shown below the editor.
    } finally {
      setCommitting(null);
    }
  };

  const showEditor = current !== null && isText(current);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="conflict-resolver"
        className={cn(
          "flex flex-col gap-3",
          showEditor ? "h-[90vh] sm:max-w-[min(96vw,1600px)]" : "max-h-[85vh] sm:max-w-3xl",
        )}
      >
        <DialogHeader>
          <DialogTitle>
            Merge <span className="font-mono">{base}</span> into{" "}
            <span className="font-mono">{branch}</span>
          </DialogTitle>
        </DialogHeader>

        {state.isLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : state.isError ? (
          <p className="text-sm text-destructive">{state.error.message}</p>
        ) : !merge?.in_progress ? (
          <div className="flex flex-col items-start gap-3 text-sm">
            <p className="text-muted-foreground">
              This merges <span className="font-mono">{base}</span> into the task branch in the
              worker&apos;s worktree without committing, so you can resolve each conflict here. The
              worker should stay idle meanwhile.
            </p>
            <Button loading={step.isPending} onClick={() => run({ action: "start" })}>
              <GitMergeIcon aria-hidden="true" />
              Start merge
            </Button>
          </div>
        ) : (
          <div className="flex min-h-0 flex-1 gap-3">
            <ul aria-label="Conflicted files" className="w-56 shrink-0 overflow-y-auto text-xs">
              {conflicts.map((file) => {
                const left = leftIn(file);
                return (
                  <li key={file.path}>
                    <button
                      type="button"
                      onClick={() => setSelected(file.path)}
                      className={cn(
                        "flex w-full items-center gap-1.5 rounded px-1.5 py-1 text-left hover:bg-accent",
                        file.path === current?.path && "bg-accent",
                      )}
                    >
                      {left > 0 ? (
                        <CircleAlertIcon
                          aria-hidden="true"
                          className="size-3.5 shrink-0 text-destructive"
                        />
                      ) : (
                        <CheckCircle2Icon
                          aria-hidden="true"
                          className="size-3.5 shrink-0 text-success"
                        />
                      )}
                      <span className="min-w-0 flex-1 truncate font-mono">{file.path}</span>
                      <span
                        data-testid="file-status"
                        className={cn(
                          "shrink-0",
                          left > 0 ? "text-destructive" : "text-muted-foreground",
                        )}
                      >
                        {!isText(file) ? "pick a side" : left > 0 ? left : "done"}
                      </span>
                    </button>
                  </li>
                );
              })}
              {picked.map((path) => (
                <li
                  key={path}
                  data-testid="resolved-file"
                  className="flex items-center gap-1.5 px-1.5 py-1 text-muted-foreground"
                >
                  <CheckCircle2Icon aria-hidden="true" className="size-3.5 shrink-0 text-success" />
                  <span className="min-w-0 flex-1 truncate font-mono">{path}</span>
                  <span className="shrink-0">done</span>
                </li>
              ))}
            </ul>
            <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
              {current && isText(current) ? (
                <Suspense fallback={<p className="text-muted-foreground">Loading editor…</p>}>
                  <MergeEditor
                    key={current.path}
                    path={current.path}
                    ours={current.ours ?? ""}
                    theirs={current.theirs ?? ""}
                    oursLabel={`Task (${branch})`}
                    base={base}
                    value={draftOf(current)}
                    onChange={(text) => setDrafts((d) => ({ ...d, [current.path]: text }))}
                  />
                </Suspense>
              ) : current ? (
                <WholeFilePick
                  file={current}
                  base={base}
                  pending={busy}
                  onPick={(side) =>
                    run({ action: "resolve", path: current.path, side }, () =>
                      setPicked((p) => [...p, current.path]),
                    )
                  }
                />
              ) : (
                <p className="text-sm text-muted-foreground">
                  Every conflict is resolved. Commit the merge to finish.
                </p>
              )}
            </div>
          </div>
        )}

        {step.isError && <p className="text-sm text-destructive">{step.error.message}</p>}

        {merge?.in_progress && (
          <div className="flex flex-wrap items-center gap-3 border-t border-border pt-3 text-xs">
            <Button
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => run({ action: "abort" })}
            >
              Abort merge
            </Button>
            <p data-testid="merge-progress" className="ml-auto text-muted-foreground">
              {ready
                ? "All conflicts resolved."
                : currentLeft > 0 && current
                  ? `${plural(currentLeft, "conflict")} left in ${current.path}` +
                    (openFiles.length > 1 ? ` · ${plural(openFiles.length, "file")} to go` : "")
                  : `${plural(openFiles.length, "file")} still to resolve`}
            </p>
            {ready ? (
              <>
                <label className="flex items-center gap-1 text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={notify}
                    onChange={(event) => setNotify(event.target.checked)}
                  />
                  Tell the worker
                </label>
                <Button loading={committing !== null} disabled={busy} onClick={() => void commit()}>
                  <GitMergeIcon aria-hidden="true" />
                  Commit merge
                </Button>
              </>
            ) : currentLeft === 0 && nextFile ? (
              <Button disabled={busy} onClick={() => setSelected(nextFile.path)}>
                Next file: <span className="font-mono">{nextFile.path}</span>
              </Button>
            ) : null}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function WholeFilePick({
  file,
  base,
  pending,
  onPick,
}: {
  file: UnmergedFile;
  base: string;
  pending: boolean;
  onPick: (side: "ours" | "theirs") => void;
}) {
  const what = file.binary ? "is a binary file" : "was deleted on one side";
  return (
    <div className="flex flex-col items-start gap-3 text-sm">
      <p className="text-muted-foreground">
        <span className="font-mono">{file.path}</span> {what}, so it can&apos;t be merged line by
        line. Keep one version whole:
      </p>
      <div className="flex gap-2">
        <Button variant="outline" disabled={pending} onClick={() => onPick("ours")}>
          {file.deleted_in_ours ? "Keep it deleted (task)" : "Keep task version"}
        </Button>
        <Button variant="outline" disabled={pending} onClick={() => onPick("theirs")}>
          {file.deleted_in_theirs ? `Keep it deleted (${base})` : `Keep ${base} version`}
        </Button>
      </div>
    </div>
  );
}
