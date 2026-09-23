// Segmented "Uncommitted | vs <base>" switch for the Changes list.
//
// "Uncommitted" is the classic working-tree-vs-HEAD view. "vs <base>" shows
// everything the session's branch changed since it forked (committed,
// uncommitted and untracked) — the review view for a task worktree.

import type { DiffSource } from "@/hooks/useBranchDiff";
import { cn } from "@/lib/utils";

interface DiffSourceToggleProps {
  source: DiffSource;
  onChange: (source: DiffSource) => void;
  /** Base branch name once known, e.g. "main". */
  base: string | null;
  /** Disables the branch option, e.g. for a non-git workspace. */
  branchUnavailableReason?: string | null;
}

/**
 * Render the Changes-list baseline switch.
 *
 * @param props See {@link DiffSourceToggleProps}.
 * @returns A two-option segmented control.
 */
export function DiffSourceToggle({
  source,
  onChange,
  base,
  branchUnavailableReason,
}: DiffSourceToggleProps) {
  const options: { value: DiffSource; label: string; title: string; disabled?: boolean }[] = [
    {
      value: "head",
      label: "Uncommitted",
      title: "Working-tree changes since the last commit",
    },
    {
      value: "branch",
      label: base ? `vs ${base}` : "vs base",
      title:
        branchUnavailableReason ??
        "Everything this branch changed since it forked: commits, edits and new files",
      disabled: branchUnavailableReason != null && source !== "branch",
    },
  ];
  return (
    <div
      role="radiogroup"
      aria-label="Compare changes against"
      className="flex shrink-0 items-center rounded-full border border-border p-[2px] text-xs"
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={source === option.value}
          title={option.title}
          disabled={option.disabled}
          onClick={() => onChange(option.value)}
          className={cn(
            "max-w-32 cursor-pointer truncate rounded-full px-2 py-[2px] transition-colors",
            source === option.value
              ? "bg-muted text-foreground"
              : "text-muted-foreground hover:text-foreground",
            option.disabled && "cursor-not-allowed opacity-50",
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
