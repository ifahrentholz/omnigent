"""Land a task branch into its base, from the checkout that has the base.

A sub-agent's work sits on its own branch in its own worktree. Landing it
means merging that branch into the base branch where the base is checked
out (usually the main checkout). Both sides must be clean: uncommitted task
changes would silently stay behind, and a dirty base checkout is someone's
work in progress. A conflicting merge is aborted, so neither tree changes,
and the conflicting files are reported.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Literal

from omnigent.runtime.filesystem_registry import _git_timeout_seconds

MergeStrategy = Literal["merge", "squash"]

# The runner's file watcher runs ``git status`` in the same checkout and briefly
# holds ``index.lock``; a write that collides with it retries instead of failing.
_LOCK_RETRIES = 20
_LOCK_RETRY_DELAY_S = 0.2


class BranchMergeError(ValueError):
    """The merge was refused or failed; maps to HTTP 409 with ``conflicts``."""

    def __init__(self, message: str, conflicts: list[str] | None = None) -> None:
        super().__init__(message)
        self.conflicts = conflicts or []


@dataclass(frozen=True)
class MergeResult:
    """
    Outcome of a successful merge.

    :param branch: The task branch that was merged, e.g. ``"omni/login-a1b2"``.
    :param base: The base branch it landed on, e.g. ``"main"``.
    :param commit: The base's new ``HEAD`` commit.
    :param checkout: Directory of the checkout the merge ran in.
    """

    branch: str
    base: str
    commit: str
    checkout: str


def _git(cwd: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    """
    Run git in ``cwd``.

    :param cwd: Working directory.
    :param args: Git arguments.
    :param check: Raise :class:`BranchMergeError` on a non-zero exit.
    :returns: The completed process.
    :raises BranchMergeError: When ``check`` is set and git fails.
    """
    for attempt in range(_LOCK_RETRIES + 1):
        try:
            result = subprocess.run(
                ["git", "-C", cwd, *args],
                capture_output=True,
                text=True,
                timeout=_git_timeout_seconds(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BranchMergeError(f"git {args[0]} could not run: {exc}") from exc
        if result.returncode == 0 or "index.lock" not in result.stderr:
            break
        if attempt < _LOCK_RETRIES:
            time.sleep(_LOCK_RETRY_DELAY_S)
    if check and result.returncode != 0:
        raise BranchMergeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _is_dirty(cwd: str, *, untracked: bool = True) -> bool:
    """
    Report whether a work tree has uncommitted changes.

    :param cwd: Work tree directory.
    :param untracked: Whether untracked files count. The base checkout
        ignores them: git refuses by itself if an untracked file would be
        overwritten, and tools drop scratch files there.
    :returns: ``True`` when ``git status`` lists anything.
    """
    mode = "all" if untracked else "no"
    status = _git(cwd, "status", "--porcelain", f"--untracked-files={mode}", check=True)
    return bool(status.stdout.strip())


def _checkout_of(cwd: str, branch: str) -> str | None:
    """
    Find the work tree that has ``branch`` checked out.

    :param cwd: Any directory inside the repository.
    :param branch: Branch name, e.g. ``"main"``.
    :returns: That work tree's path, or ``None`` when no work tree has it.
    """
    listing = _git(cwd, "worktree", "list", "--porcelain", check=True).stdout
    path: str | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = line.removeprefix("worktree ")
        elif line == f"branch refs/heads/{branch}" and path is not None:
            return path
    return None


def merge_task_branch(
    worktree: str,
    base: str,
    *,
    strategy: MergeStrategy = "merge",
    message: str | None = None,
) -> MergeResult:
    """
    Merge the branch checked out in ``worktree`` into ``base``.

    :param worktree: The task's worktree directory.
    :param base: Base branch to land on, e.g. the session's ``git_base_branch``.
    :param strategy: ``"merge"`` (``--no-ff``, keeps the task's commits) or
        ``"squash"`` (one commit on the base).
    :param message: Commit message; defaults to ``"Merge <branch> into <base>"``.
    :returns: The merge outcome.
    :raises BranchMergeError: When a side is dirty, the base is not checked
        out anywhere, the branches share no history, or the merge conflicts
        (with the conflicting paths; both trees are left unchanged).
    """
    branch = _git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if not branch:
        raise BranchMergeError("the task worktree has no branch checked out (detached HEAD)")
    if branch == base:
        raise BranchMergeError(f"the task worktree is on the base branch {base!r} itself")
    if _is_dirty(worktree):
        raise BranchMergeError(
            "the task worktree has uncommitted changes; commit or discard them first"
        )
    checkout = _checkout_of(worktree, base)
    if checkout is None:
        raise BranchMergeError(f"no checkout has {base!r} checked out to merge into")
    if _is_dirty(checkout, untracked=False):
        raise BranchMergeError(f"the {base!r} checkout at {checkout} has uncommitted changes")
    subject = message or f"Merge {branch} into {base}"
    if strategy == "squash":
        attempt = _git(checkout, "merge", "--squash", "--end-of-options", branch)
    else:
        attempt = _git(checkout, "merge", "--no-ff", "-m", subject, "--end-of-options", branch)
    if attempt.returncode != 0:
        conflicts = [
            path
            for path in _git(
                checkout, "diff", "--name-only", "--diff-filter=U"
            ).stdout.splitlines()
            if path
        ]
        # Restore the base checkout exactly (index and files).
        _git(checkout, "merge", "--abort")
        _git(checkout, "reset", "--hard", "--quiet", "HEAD")
        if conflicts:
            raise BranchMergeError(
                f"merging {branch} into {base} conflicts; nothing was changed", conflicts
            )
        raise BranchMergeError(f"git merge failed: {(attempt.stderr or attempt.stdout).strip()}")
    if strategy == "squash":
        if not _git(checkout, "diff", "--cached", "--name-only").stdout.strip():
            raise BranchMergeError(f"{branch} has no changes relative to {base}")
        try:
            _git(checkout, "commit", "--quiet", "-m", subject, check=True)
        except BranchMergeError:
            # Don't leave the squashed changes staged in someone's checkout.
            _git(checkout, "reset", "--hard", "--quiet", "HEAD")
            raise
    commit = _git(checkout, "rev-parse", "HEAD", check=True).stdout.strip()
    return MergeResult(branch=branch, base=base, commit=commit, checkout=checkout)
