"""Find and remove sub-agent worktrees nobody needs any more.

Sub-agent worktrees (branches ``omni/*``) outlive their sessions on purpose,
so the branch stays reviewable. They pile up when a session is archived or
deleted without ``delete_branch``, or when a create timed out after the host
had already added the worktree. ``omnigent worktrees prune`` lists them and
removes them after confirmation:

- A worktree is a candidate when no session uses it any more, or its session
  is archived. Worktrees of live sessions are never touched.
- A worktree with uncommitted or untracked changes is skipped unless forced.
- The branch is kept, so the work stays reachable. Only a branch whose work is
  already on its base (merged or squashed), or that never changed anything,
  can be deleted with ``--delete-branch``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnigent.host.git_worktree import WorktreeError, list_worktrees
from omnigent.runner.branch_diff import BranchDiffError, branch_changes

SUBAGENT_BRANCH_PREFIX = "omni/"


@dataclass(frozen=True)
class PruneCandidate:
    """
    A worktree that ``prune`` may remove.

    :param path: Worktree directory.
    :param branch: Checked-out branch, e.g. ``"omni/login-a1b2c3"``.
    :param reason: Why it is a candidate: ``"no session"`` or
        ``"session archived"``.
    :param session_id: The archived session, or ``None``.
    :param dirty: Uncommitted or untracked changes exist.
    :param landed: The branch's work is on its base, or it changed nothing.
    """

    path: str
    branch: str
    reason: str
    session_id: str | None
    dirty: bool
    landed: bool


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
    """
    Run git in ``cwd``.

    :param cwd: Working directory.
    :param args: Git arguments.
    :returns: The completed process.
    """
    return subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True, check=False, timeout=60
    )


def _same_path(left: str, right: str) -> bool:
    """
    Compare two paths after resolving symlinks.

    :param left: A path.
    :param right: Another path.
    :returns: ``True`` when both name the same directory.
    """
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except OSError:
        return False


def _is_dirty(path: str) -> bool:
    """
    Report uncommitted or untracked changes in a worktree.

    :param path: Worktree directory.
    :returns: ``True`` when ``git status`` lists anything, or git fails.
    """
    result = _git(path, "status", "--porcelain")
    return result.returncode != 0 or bool(result.stdout.strip())


def _is_landed(path: str, base: str | None) -> bool:
    """
    Report whether a branch's work is already on its base, or it has none.

    :param path: Worktree directory.
    :param base: The session's base branch, or ``None`` to infer it.
    :returns: ``True`` when deleting the branch loses nothing.
    """
    try:
        changes = branch_changes(path, base=base)
    except BranchDiffError:
        return False
    return bool(changes["landed"]) or not changes["data"]


def find_prune_candidates(
    repo_path: str,
    sessions: Iterable[Mapping[str, Any]],
) -> list[PruneCandidate]:
    """
    List the repo's sub-agent worktrees that no live session uses.

    :param repo_path: A directory inside the repository.
    :param sessions: Every session the server knows, archived ones
        included, with ``id``, ``workspace``, ``archived`` and
        ``git_base_branch``.
    :returns: The candidates, in ``git worktree list`` order.
    :raises WorktreeError: When ``repo_path`` is not inside a git repository.
    """
    known = list(sessions)
    candidates: list[PruneCandidate] = []
    for worktree in list_worktrees(repo_path=repo_path):
        branch = worktree.branch
        if worktree.is_main or branch is None or not branch.startswith(SUBAGENT_BRANCH_PREFIX):
            continue
        if not Path(worktree.path).is_dir():
            continue
        owner = next(
            (
                session
                for session in known
                if session.get("workspace")
                and _same_path(str(session["workspace"]), worktree.path)
            ),
            None,
        )
        if owner is not None and not owner.get("archived"):
            continue
        base = owner.get("git_base_branch") if owner is not None else None
        landed = _is_landed(worktree.path, str(base) if base else None)
        candidates.append(
            PruneCandidate(
                path=worktree.path,
                branch=branch,
                reason="session archived" if owner is not None else "no session",
                session_id=str(owner["id"]) if owner is not None else None,
                dirty=_is_dirty(worktree.path),
                landed=landed,
            )
        )
    return candidates


def prune_worktree(
    candidate: PruneCandidate,
    *,
    force: bool = False,
    delete_branch: bool = False,
) -> bool:
    """
    Remove one candidate's worktree and, if safe and asked, its branch.

    :param candidate: The worktree to remove.
    :param force: Remove even with uncommitted or untracked changes.
    :param delete_branch: Also delete the branch when it has landed.
    :returns: ``True`` when the branch was deleted too.
    :raises WorktreeError: When git refuses, e.g. a dirty worktree without
        ``force``.
    """
    common = _git(candidate.path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode != 0:
        raise WorktreeError(f"not a git worktree: {candidate.path}")
    main_repo = str(Path(common.stdout.strip()).parent)
    remove = ["worktree", "remove", *(["--force"] if force else []), candidate.path]
    result = _git(main_repo, *remove)
    if result.returncode != 0:
        raise WorktreeError(f"git worktree remove failed: {result.stderr.strip()}")
    if not (delete_branch and candidate.landed):
        return False
    result = _git(main_repo, "branch", "-D", "--end-of-options", candidate.branch)
    if result.returncode != 0:
        raise WorktreeError(f"git branch -D failed: {result.stderr.strip()}")
    return True


def prune_stale_registrations(repo_path: str) -> None:
    """
    Drop registrations of worktrees whose directory was deleted by hand.

    :param repo_path: A directory inside the repository.
    """
    _git(repo_path, "worktree", "prune")
