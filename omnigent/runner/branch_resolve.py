"""Resolve a task branch's conflicts with its base by hand, in its worktree.

The reviewer merges the base into the task branch without committing, fixes
each conflicting file (or takes one side whole), and commits the merge; or
aborts, which restores the worktree. Conflicting files are written with diff3
markers so the reviewer sees the common ancestor too.

Only files git reports as unmerged can be written, so the endpoint cannot be
used to edit arbitrary files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Literal

from omnigent.runner.branch_merge import _git
from omnigent.runtime.filesystem_registry import _git_timeout_seconds

Side = Literal["ours", "theirs"]
# Files above this size are offered as whole-file choices only.
_MAX_TEXT_BYTES = 1024 * 1024
_MARKER_PATTERN = r"^(<<<<<<<|>>>>>>>) "


class BranchResolveError(ValueError):
    """The step was refused or failed; maps to HTTP 409."""


def _merge_in_progress(root: str) -> bool:
    return _git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0


def _unmerged_paths(root: str) -> list[str]:
    result = _git(root, "diff", "--name-only", "--diff-filter=U", "-z")
    return sorted(filter(None, result.stdout.split("\0")))


def _stage(root: str, stage: int, path: str) -> bytes | None:
    """
    Read one index stage of an unmerged file.

    :param root: The worktree.
    :param stage: 1 = common ancestor, 2 = ours (task), 3 = theirs (base).
    :param path: Repository-relative path.
    :returns: The content, or ``None`` when the stage is absent (add/delete).
    """
    result = subprocess.run(
        ["git", "-C", root, "show", f":{stage}:{path}"],
        capture_output=True,
        timeout=_git_timeout_seconds(),
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _text(content: bytes | None) -> str | None:
    """Decode a stage as text; ``None`` for absent, binary or oversized content."""
    if content is None or len(content) > _MAX_TEXT_BYTES or b"\0" in content:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _conflict_entry(root: str, path: str) -> dict[str, Any]:
    ours, theirs, ancestor = (_stage(root, n, path) for n in (2, 3, 1))
    working_path = Path(root) / path
    working = working_path.read_bytes() if working_path.is_file() else None
    texts = [_text(c) for c in (ours, theirs, ancestor, working)]
    binary = any(
        c is not None and t is None
        for c, t in zip((ours, theirs, ancestor, working), texts, strict=True)
    )
    return {
        "path": path,
        "binary": binary,
        "ours": None if binary else texts[0],
        "theirs": None if binary else texts[1],
        "ancestor": None if binary else texts[2],
        "working": None if binary else texts[3],
        "deleted_in_ours": ours is None,
        "deleted_in_theirs": theirs is None,
    }


def merge_state(root: str, *, base: str) -> dict[str, Any]:
    """
    Describe the worktree's merge.

    :param root: The task's worktree.
    :param base: The base branch being merged in, e.g. ``"main"``.
    :returns: ``{in_progress, base, branch, conflicts: [...]}``.
    """
    branch = _git(root, "branch", "--show-current").stdout.strip()
    in_progress = _merge_in_progress(root)
    conflicts = (
        [_conflict_entry(root, path) for path in _unmerged_paths(root)] if in_progress else []
    )
    return {
        "object": "git.resolve",
        "in_progress": in_progress,
        "base": base,
        "branch": branch,
        "conflicts": conflicts,
    }


def start_merge(root: str, *, base: str) -> dict[str, Any]:
    """
    Merge ``base`` into the task branch without committing.

    :param root: The task's worktree.
    :param base: The base branch, e.g. ``"main"``.
    :returns: The resulting :func:`merge_state`.
    :raises BranchResolveError: When a merge already runs, the tree has
        uncommitted changes, or git fails for another reason than conflicts.
    """
    if _merge_in_progress(root):
        raise BranchResolveError("a merge is already in progress in this worktree")
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        raise BranchResolveError(
            "the worktree has uncommitted changes; commit or discard them first"
        )
    result = _git(
        root,
        "-c",
        "merge.conflictStyle=diff3",
        "merge",
        "--no-ff",
        "--no-commit",
        "--end-of-options",
        base,
    )
    if result.returncode != 0 and not _merge_in_progress(root):
        raise BranchResolveError(
            f"git merge {base} failed: {(result.stderr or result.stdout).strip()}"
        )
    return merge_state(root, base=base)


def resolve_file(
    root: str,
    *,
    base: str,
    path: str,
    content: str | None = None,
    side: Side | None = None,
) -> dict[str, Any]:
    """
    Resolve one unmerged file and stage it.

    :param root: The task's worktree.
    :param base: The base branch, for the returned state.
    :param path: An unmerged path.
    :param content: The resolved text, or ``None`` when taking a ``side``.
    :param side: ``"ours"`` (task) or ``"theirs"`` (base) to keep that version whole.
    :returns: The updated :func:`merge_state`.
    :raises BranchResolveError: When ``path`` is not unmerged or the input is invalid.
    """
    if path not in _unmerged_paths(root):
        raise BranchResolveError(f"{path!r} has no unresolved conflict")
    if (content is None) == (side is None):
        raise BranchResolveError("pass either the resolved content or a side")
    if side is not None:
        stage = 2 if side == "ours" else 3
        kept = _stage(root, stage, path)
        if kept is None:
            _git(root, "rm", "-q", "--end-of-options", path)
            return merge_state(root, base=base)
        (Path(root) / path).write_bytes(kept)
    else:
        (Path(root) / path).write_text(content or "", encoding="utf-8")
    added = _git(root, "add", "--end-of-options", path)
    if added.returncode != 0:
        raise BranchResolveError(f"git add {path} failed: {added.stderr.strip()}")
    return merge_state(root, base=base)


def complete_merge(root: str, *, base: str, message: str | None = None) -> dict[str, Any]:
    """
    Commit the resolved merge.

    :param root: The task's worktree.
    :param base: The base branch.
    :param message: Commit message; git's prepared merge message when ``None``.
    :returns: ``{committed: True, commit, branch, base, resolved: [paths]}``.
    :raises BranchResolveError: When no merge runs, files are still unmerged,
        or staged files still contain conflict markers.
    """
    if not _merge_in_progress(root):
        raise BranchResolveError("no merge is in progress in this worktree")
    unmerged = _unmerged_paths(root)
    if unmerged:
        raise BranchResolveError(f"resolve every file first: {', '.join(unmerged)}")
    staged = [
        p for p in _git(root, "diff", "--cached", "--name-only", "-z").stdout.split("\0") if p
    ]
    if staged:
        markers = _git(
            root, "grep", "--cached", "-l", "-E", _MARKER_PATTERN, "--", *staged
        ).stdout.split()
        if markers:
            raise BranchResolveError(f"conflict markers are left in: {', '.join(markers)}")
    args = ["commit", "--no-verify"] + (["-m", message] if message else ["--no-edit"])
    committed = _git(root, *args)
    if committed.returncode != 0:
        raise BranchResolveError(
            f"git commit failed: {(committed.stderr or committed.stdout).strip()}"
        )
    return {
        "committed": True,
        "commit": _git(root, "rev-parse", "HEAD").stdout.strip(),
        "branch": _git(root, "branch", "--show-current").stdout.strip(),
        "base": base,
        "resolved": staged,
    }


def abort_merge(root: str, *, base: str) -> dict[str, Any]:
    """
    Abandon the merge and restore the worktree.

    :param root: The task's worktree.
    :param base: The base branch, for the returned state.
    :returns: The :func:`merge_state` afterwards.
    :raises BranchResolveError: When no merge is in progress.
    """
    if not _merge_in_progress(root):
        raise BranchResolveError("no merge is in progress in this worktree")
    aborted = _git(root, "merge", "--abort")
    if aborted.returncode != 0:
        raise BranchResolveError(f"git merge --abort failed: {aborted.stderr.strip()}")
    return merge_state(root, base=base)


def branch_resolve(root: str, action: str, *, base: str, **params: Any) -> dict[str, Any]:
    """
    Dispatch one step of a manual conflict resolution.

    :param root: The task's worktree.
    :param action: ``"status"``, ``"start"``, ``"resolve"``, ``"complete"`` or ``"abort"``.
    :param base: The base branch.
    :param params: The step's fields (``path``, ``content``, ``side``, ``message``).
    :returns: The step's result.
    :raises BranchResolveError: On an unknown action or a refused step.
    """
    if action == "status":
        return merge_state(root, base=base)
    if action == "start":
        return start_merge(root, base=base)
    if action == "resolve":
        side = params.get("side")
        if side not in (None, "ours", "theirs"):
            raise BranchResolveError("side must be 'ours' or 'theirs'")
        content = params.get("content")
        if content is not None and not isinstance(content, str):
            raise BranchResolveError("content must be a string")
        return resolve_file(
            root, base=base, path=str(params.get("path") or ""), content=content, side=side
        )
    if action == "complete":
        return complete_merge(root, base=base, message=params.get("message") or None)
    if action == "abort":
        return abort_merge(root, base=base)
    raise BranchResolveError(f"unknown action {action!r}")
