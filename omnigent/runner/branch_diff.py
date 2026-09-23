"""Everything a task's branch changed, relative to the branch it forked from.

The Changes panel diffs the working tree against ``HEAD``, so a worker that
commits in its worktree makes its own changes disappear from view. Reviewing
a task needs the full picture: committed *and* uncommitted *and* untracked
changes since ``merge-base(base, HEAD)``.

Pure, read-only git helpers shared by the runner endpoints and the host's
runner-offline fallback (:class:`omnigent.workspace_fs.WorkspaceReader`), so
both return identical JSON. The local base branch wins over ``origin/<base>``:
a task forked from an unpushed local branch must not show that branch's own
commits as task changes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from omnigent.entities.environment_filesystem import InvalidPath
from omnigent.runner.environment_filesystem import _validate_path
from omnigent.runtime.filesystem_registry import _git_timeout_seconds

_MAX_READ_BYTES = 10 * 1024 * 1024
_MAX_COUNTED_UNTRACKED_BYTES = 1024 * 1024
_DEFAULT_BASE_CANDIDATES = ("main", "master")

# ``git diff --name-status`` letters → the web list's status vocabulary.
_STATUS_MAP = {
    "A": "created",
    "C": "created",
    "D": "deleted",
    "M": "modified",
    "R": "renamed",
    "T": "modified",
    "U": "modified",
}


class BranchDiffError(ValueError):
    """A branch diff cannot be computed; maps to HTTP 400 (bad base/repo)."""


def _git(root: str, *args: str) -> tuple[int, str]:
    """
    Run a read-only git command in ``root``.

    :param root: Absolute workspace directory.
    :param args: Git arguments, e.g. ``("merge-base", "main", "HEAD")``.
    :returns: ``(returncode, stdout)``; ``-1`` when git could not run.
    """
    try:
        result = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            timeout=_git_timeout_seconds(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""
    return result.returncode, result.stdout.decode("utf-8", errors="replace")


def _commit_exists(root: str, ref: str) -> bool:
    """
    Report whether ``ref`` names a commit.

    :param root: Absolute workspace directory.
    :param ref: Ref to probe, e.g. ``"origin/main"``.
    :returns: ``True`` when it resolves to a commit.
    """
    rc, _ = _git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
    return rc == 0


def resolve_base(root: str, base: str | None) -> str:
    """
    Pick the base branch to diff against.

    :param root: Absolute workspace directory.
    :param base: Explicit base, e.g. the session's ``git_base_branch``.
    :returns: The base branch name.
    :raises BranchDiffError: When no base is given and none can be inferred.
    """
    if base:
        return base
    rc, out = _git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and out.strip():
        return out.strip().removeprefix("origin/")
    for candidate in _DEFAULT_BASE_CANDIDATES:
        if _commit_exists(root, candidate):
            return candidate
    raise BranchDiffError("no base branch given and none could be inferred; pass ?base=")


def _merge_base(root: str, base: str) -> str:
    """
    Resolve the commit the current branch forked from ``base``.

    :param root: Absolute workspace directory.
    :param base: Base branch name, e.g. ``"main"``.
    :returns: The merge-base commit SHA.
    :raises BranchDiffError: When ``root`` is no git repo or ``base`` is unknown.
    """
    rc, _ = _git(root, "rev-parse", "--is-inside-work-tree")
    if rc != 0:
        raise BranchDiffError("workspace is not a git repository")
    for candidate in (base, f"origin/{base}"):
        if not _commit_exists(root, candidate):
            continue
        rc, out = _git(root, "merge-base", "--end-of-options", candidate, "HEAD")
        if rc == 0 and out.strip():
            return out.strip()
    raise BranchDiffError(f"base branch {base!r} has no common history with HEAD")


def _parse_name_status(raw: str) -> list[tuple[str, str, str | None]]:
    """
    Parse ``git diff --name-status -z -M`` output.

    :param raw: NUL-separated git output.
    :returns: ``(status_letter, path, previous_path)`` tuples.
    """
    tokens = raw.split("\0")
    records: list[tuple[str, str, str | None]] = []
    index = 0
    while index < len(tokens) and tokens[index]:
        letter = tokens[index][0]
        if letter in ("R", "C"):
            records.append((letter, tokens[index + 2], tokens[index + 1]))
            index += 3
        else:
            records.append((letter, tokens[index + 1], None))
            index += 2
    return records


def _parse_numstat(raw: str) -> dict[str, tuple[int | None, int | None]]:
    """
    Parse ``git diff --numstat -z -M`` output into per-path line counts.

    :param raw: NUL-separated git output.
    :returns: ``{path: (added, removed)}``; ``None`` counts for binaries.
    """
    tokens = raw.split("\0")
    counts: dict[str, tuple[int | None, int | None]] = {}
    index = 0
    while index < len(tokens) and tokens[index]:
        added, removed, path = tokens[index].split("\t", 2)
        if path:
            index += 1
        else:
            # Renames put ``old`` and ``new`` in the next two tokens.
            path = tokens[index + 2]
            index += 3
        counts[path] = (
            int(added) if added.isdigit() else None,
            int(removed) if removed.isdigit() else None,
        )
    return counts


def _stat(root: Path, relative: str) -> tuple[int | None, int | None]:
    """
    Read size and mtime of a workspace file.

    :param root: Workspace root.
    :param relative: Path relative to ``root``.
    :returns: ``(bytes, modified_at)``, ``(None, None)`` when missing.
    """
    try:
        info = (root / relative).stat()
    except OSError:
        return None, None
    return info.st_size, int(info.st_mtime)


def _count_lines(path: Path) -> int | None:
    """
    Count lines of an untracked text file, like numstat does for new files.

    :param path: Absolute file path.
    :returns: The line count, or ``None`` for binary or oversized files.
    """
    try:
        if path.stat().st_size > _MAX_COUNTED_UNTRACKED_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _entry(
    root: Path,
    path: str,
    status: str,
    lines: tuple[int | None, int | None],
    previous_path: str | None = None,
) -> dict[str, Any]:
    """
    Build one changed-file entry in the Changes panel's shape.

    :param root: Workspace root.
    :param path: Path relative to ``root``.
    :param status: ``"created"``, ``"modified"``, ``"deleted"``, ``"renamed"``.
    :param lines: ``(lines_added, lines_removed)``.
    :param previous_path: Old path of a rename.
    :returns: The entry dict.
    """
    size, mtime = _stat(root, path)
    return {
        "object": "session.environment.filesystem.entry",
        "path": path,
        "name": path.rsplit("/", 1)[-1],
        "status": status,
        "previous_path": previous_path,
        "bytes": size,
        "modified_at": mtime,
        "lines_added": lines[0],
        "lines_removed": lines[1],
    }


def branch_changes(
    root: str,
    *,
    session_id: str | None = None,
    base: str | None = None,
) -> dict[str, Any]:
    """
    List every file the current branch changed since it forked from ``base``.

    :param root: Absolute workspace directory, e.g. a task worktree.
    :param session_id: Unused; accepted for the shared call convention.
    :param base: Base branch, e.g. ``"main"``; inferred when ``None``.
    :returns: A list payload plus ``base`` and ``merge_base``.
    :raises BranchDiffError: On a non-git workspace or unresolvable base.
    """
    del session_id
    base_name = resolve_base(root, base)
    merge_base = _merge_base(root, base_name)
    workspace = Path(root)
    rc, name_status = _git(root, "diff", "--name-status", "-z", "-M", merge_base, "--")
    if rc != 0:
        raise BranchDiffError("git diff failed")
    _, numstat = _git(root, "diff", "--numstat", "-z", "-M", merge_base, "--")
    counts = _parse_numstat(numstat)
    data = [
        _entry(
            workspace,
            path,
            _STATUS_MAP.get(letter, "modified"),
            counts.get(path, (None, None)),
            previous,
        )
        for letter, path, previous in _parse_name_status(name_status)
    ]
    _, untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    for path in filter(None, untracked.split("\0")):
        data.append(_entry(workspace, path, "created", (_count_lines(workspace / path), 0)))
    data.sort(key=lambda item: item["path"])
    return {
        "object": "list",
        "data": data,
        "has_more": False,
        "base": base_name,
        "merge_base": merge_base,
    }


def branch_file_diff(
    root: str,
    relative_path: str,
    *,
    session_id: str | None = None,
    base: str | None = None,
    previous_path: str | None = None,
) -> dict[str, Any]:
    """
    Return one file's content at the merge-base and in the working tree.

    :param root: Absolute workspace directory.
    :param relative_path: Path relative to ``root``.
    :param session_id: Unused; accepted for the shared call convention.
    :param base: Base branch, e.g. ``"main"``; inferred when ``None``.
    :param previous_path: Old path when the file was renamed.
    :returns: A file-diff dict with ``before``/``after`` (``None`` when absent).
    :raises BranchDiffError: On an invalid path or unresolvable base.
    """
    del session_id
    try:
        path = _validate_path(relative_path)
        old_path = _validate_path(previous_path) if previous_path else path
    except InvalidPath as exc:
        raise BranchDiffError(str(exc)) from exc
    if not path:
        raise BranchDiffError("cannot diff the workspace root")
    base_name = resolve_base(root, base)
    merge_base = _merge_base(root, base_name)
    rc, before = _git(root, "show", f"{merge_base}:{old_path}")
    workspace = Path(root).resolve()
    target = (workspace / path).resolve()
    after: str | None = None
    if target.is_relative_to(workspace) and target.is_file():
        with target.open("rb") as handle:
            after = handle.read(_MAX_READ_BYTES).decode("utf-8", errors="replace")
    return {
        "object": "session.environment.filesystem.file_diff",
        "path": path,
        "previous_path": previous_path,
        "before": before[:_MAX_READ_BYTES] if rc == 0 else None,
        "after": after,
        "base": base_name,
        "merge_base": merge_base,
    }


__all__ = ["BranchDiffError", "branch_changes", "branch_file_diff", "resolve_base"]
