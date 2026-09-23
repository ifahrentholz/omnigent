"""Tests for the task-branch diff (merge-base vs working tree).

A task worktree's review view must show committed, uncommitted and
untracked changes since the branch forked from its base, and nothing the
base itself did.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnigent.runner.branch_diff import (
    BranchDiffError,
    branch_changes,
    branch_file_diff,
    resolve_base,
)


def _git(repo: Path, *args: str) -> str:
    """
    Run git in ``repo`` with a fixed identity.

    :param repo: Repository directory.
    :param args: Git arguments.
    :returns: Stripped stdout.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def task_repo(tmp_path: Path) -> Path:
    """
    Build a repo whose ``task`` branch forked from ``main``, then diverged.

    ``main`` gains a commit after the fork (must not show up), and the task
    has one commit, one uncommitted edit, one untracked file, a rename and
    a deletion.

    :param tmp_path: Pytest temporary directory.
    :returns: The repository path, checked out on ``task``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("a = 1\nb = 2\n")
    (repo / "old_name.py").write_text("x = 1\ny = 2\nz = 3\n")
    (repo / "gone.txt").write_text("bye\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "task")
    (repo / "app.py").write_text("a = 1\nb = 3\nc = 4\n")
    _git(repo, "mv", "old_name.py", "new_name.py")
    _git(repo, "rm", "-q", "gone.txt")
    _git(repo, "commit", "-q", "-am", "task work")
    _git(repo, "checkout", "-q", "main")
    (repo / "main_only.py").write_text("later = True\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "main moves on")
    _git(repo, "checkout", "-q", "task")
    (repo / "app.py").write_text("a = 1\nb = 3\nc = 4\nd = 5\n")
    (repo / "notes.md").write_text("one\ntwo\n")
    return repo


def test_changes_cover_committed_uncommitted_untracked(task_repo: Path) -> None:
    """
    Every kind of task change is listed with line counts, and a commit
    made on ``main`` after the fork is not.

    :param task_repo: Diverged repository fixture.
    """
    result = branch_changes(str(task_repo), base="main")
    entries = {entry["path"]: entry for entry in result["data"]}

    assert set(entries) == {"app.py", "new_name.py", "gone.txt", "notes.md"}
    assert result["base"] == "main"
    assert result["merge_base"] == _git(task_repo, "merge-base", "main", "task")
    app = entries["app.py"]
    assert (app["status"], app["lines_added"], app["lines_removed"]) == ("modified", 3, 1)
    assert entries["new_name.py"]["status"] == "renamed"
    assert entries["new_name.py"]["previous_path"] == "old_name.py"
    assert entries["gone.txt"]["status"] == "deleted"
    notes = entries["notes.md"]
    assert (notes["status"], notes["lines_added"], notes["lines_removed"]) == ("created", 2, 0)


def test_file_diff_before_is_merge_base_after_is_disk(task_repo: Path) -> None:
    """
    ``before`` is the file at the fork point and ``after`` the working tree,
    so the diff spans committed and uncommitted edits.

    :param task_repo: Diverged repository fixture.
    """
    diff = branch_file_diff(str(task_repo), "app.py", base="main")
    assert diff["before"] == "a = 1\nb = 2\n"
    assert diff["after"] == "a = 1\nb = 3\nc = 4\nd = 5\n"

    renamed = branch_file_diff(
        str(task_repo), "new_name.py", base="main", previous_path="old_name.py"
    )
    assert renamed["before"] == renamed["after"] == "x = 1\ny = 2\nz = 3\n"
    deleted = branch_file_diff(str(task_repo), "gone.txt", base="main")
    assert (deleted["before"], deleted["after"]) == ("bye\n", None)
    created = branch_file_diff(str(task_repo), "notes.md", base="main")
    assert (created["before"], created["after"]) == (None, "one\ntwo\n")


def test_local_base_wins_over_origin(tmp_path: Path, task_repo: Path) -> None:
    """
    A task forked from local ``main`` that is ahead of ``origin/main`` must
    not list local-only ``main`` commits as its own changes.

    :param tmp_path: Pytest temporary directory.
    :param task_repo: Diverged repository fixture.
    """
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(task_repo, "remote", "add", "origin", str(remote))
    first = _git(task_repo, "rev-list", "--max-parents=0", "main")
    _git(task_repo, "push", "-q", "origin", f"{first}:refs/heads/main")
    _git(task_repo, "fetch", "-q", "origin")
    _git(task_repo, "stash", "-q", "--include-untracked")
    _git(task_repo, "checkout", "-q", "main")
    (task_repo / "local_only.py").write_text("unpushed = 1\n")
    _git(task_repo, "add", ".")
    _git(task_repo, "commit", "-q", "-m", "unpushed on main")
    _git(task_repo, "checkout", "-q", "-b", "task2")
    (task_repo / "task2.py").write_text("mine = 1\n")

    paths = {entry["path"] for entry in branch_changes(str(task_repo), base="main")["data"]}
    assert paths == {"task2.py"}


def test_default_base_is_inferred(task_repo: Path) -> None:
    """
    Without a stored base, ``main`` (or ``origin/HEAD``) is used.

    :param task_repo: Diverged repository fixture.
    """
    assert resolve_base(str(task_repo), None) == "main"


@pytest.mark.parametrize(
    ("path", "base"),
    [("../escape.txt", "main"), ("app.py", "does-not-exist"), ("app.py", "--output=/tmp/x")],
)
def test_invalid_path_or_base_raises(task_repo: Path, path: str, base: str) -> None:
    """
    Traversal, unknown bases and option-looking bases are rejected.

    :param task_repo: Diverged repository fixture.
    :param path: Requested file path.
    :param base: Requested base branch.
    """
    with pytest.raises(BranchDiffError):
        branch_file_diff(str(task_repo), path, base=base)


def test_non_git_workspace_raises(tmp_path: Path) -> None:
    """
    A plain directory has no branch to diff.

    :param tmp_path: Pytest temporary directory (no repo).
    """
    with pytest.raises(BranchDiffError, match="not a git repository"):
        branch_changes(str(tmp_path), base="main")


def test_host_fallback_serves_the_same_payload(task_repo: Path) -> None:
    """
    With the runner offline the host answers from the same helpers, so the
    UI cannot tell which side served the review view.

    :param task_repo: Diverged repository fixture.
    """
    from omnigent.host.connect import HostProcess
    from omnigent.workspace_fs import WorkspaceReader, WorkspaceReaderError

    reader = WorkspaceReader(task_repo)
    changes = HostProcess._dispatch_fs_op(reader, "branch_changes", "conv_x", {"base": "main"})
    assert changes == branch_changes(str(task_repo), base="main")
    diff = HostProcess._dispatch_fs_op(
        reader, "branch_diff", "conv_x", {"path": "app.py", "base": "main"}
    )
    assert diff == branch_file_diff(str(task_repo), "app.py", base="main")
    with pytest.raises(WorkspaceReaderError) as excinfo:
        reader.branch_changes("no-such-base")
    assert excinfo.value.status == 400
