"""Tests for resolving a task branch's conflicts with its base by hand."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from omnigent.runner.branch_resolve import BranchResolveError, branch_resolve


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def conflicted(tmp_path: Path) -> Path:
    """
    A repo on branch ``task`` whose README heading conflicts with ``main``.

    ``notes.txt`` changes on ``main`` only and merges cleanly.

    :param tmp_path: Pytest temporary directory.
    :returns: The repository, checked out on ``task``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@example.com")
    (repo / "README.md").write_text("# Demo\n\nHello\n")
    (repo / "notes.txt").write_text("one\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "task")
    (repo / "README.md").write_text("# Demo B\n\nHello\n")
    _git(repo, "commit", "-qam", "task heading")
    _git(repo, "checkout", "-q", "main")
    (repo / "README.md").write_text("# Demo A\n\nHello\n")
    (repo / "notes.txt").write_text("one\ntwo\n")
    _git(repo, "commit", "-qam", "main heading")
    _git(repo, "checkout", "-q", "task")
    return repo


def test_start_leaves_the_conflict_with_all_three_versions(conflicted: Path) -> None:
    state = branch_resolve(str(conflicted), "start", base="main")

    assert state["in_progress"] is True
    assert state["branch"] == "task"
    [entry] = state["conflicts"]
    assert entry["path"] == "README.md"
    assert (entry["ours"], entry["theirs"], entry["ancestor"]) == (
        "# Demo B\n\nHello\n",
        "# Demo A\n\nHello\n",
        "# Demo\n\nHello\n",
    )
    assert "<<<<<<<" in entry["working"] and "|||||||" in entry["working"]
    assert entry["binary"] is False


def test_resolving_and_completing_commits_a_merge_of_the_base(conflicted: Path) -> None:
    branch_resolve(str(conflicted), "start", base="main")
    state = branch_resolve(
        str(conflicted),
        "resolve",
        base="main",
        path="README.md",
        content="# Demo A & B\n\nHello\n",
    )
    assert state["conflicts"] == []

    done = branch_resolve(str(conflicted), "complete", base="main")

    assert done["committed"] is True
    assert set(done["resolved"]) == {"README.md", "notes.txt"}
    parents = _git(conflicted, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert len(parents) == 3, "a merge commit"
    _git(conflicted, "merge-base", "--is-ancestor", "main", "HEAD")
    assert (conflicted / "README.md").read_text() == "# Demo A & B\n\nHello\n"
    assert (conflicted / "notes.txt").read_text() == "one\ntwo\n"
    assert branch_resolve(str(conflicted), "status", base="main")["in_progress"] is False


def test_complete_refuses_unresolved_files_and_leftover_markers(conflicted: Path) -> None:
    branch_resolve(str(conflicted), "start", base="main")
    with pytest.raises(BranchResolveError, match=re.escape("resolve every file first: README.md")):
        branch_resolve(str(conflicted), "complete", base="main")

    branch_resolve(
        str(conflicted),
        "resolve",
        base="main",
        path="README.md",
        content="<<<<<<< ours\n# Demo B\n=======\n# Demo A\n>>>>>>> theirs\n",
    )
    with pytest.raises(
        BranchResolveError, match=re.escape("conflict markers are left in: README.md")
    ):
        branch_resolve(str(conflicted), "complete", base="main")


def test_only_unmerged_files_can_be_written(conflicted: Path) -> None:
    branch_resolve(str(conflicted), "start", base="main")
    for path in ("notes.txt", "../outside.txt", "new.txt"):
        with pytest.raises(BranchResolveError, match="no unresolved conflict"):
            branch_resolve(str(conflicted), "resolve", base="main", path=path, content="x")
    assert not (conflicted.parent / "outside.txt").exists()


def test_taking_a_side_keeps_that_version_whole(conflicted: Path) -> None:
    branch_resolve(str(conflicted), "start", base="main")
    branch_resolve(str(conflicted), "resolve", base="main", path="README.md", side="theirs")
    branch_resolve(str(conflicted), "complete", base="main", message="take main's heading")

    assert (conflicted / "README.md").read_text() == "# Demo A\n\nHello\n"
    assert _git(conflicted, "log", "-1", "--format=%s") == "take main's heading"


def test_abort_restores_the_worktree(conflicted: Path) -> None:
    head = _git(conflicted, "rev-parse", "HEAD")
    branch_resolve(str(conflicted), "start", base="main")

    state = branch_resolve(str(conflicted), "abort", base="main")

    assert state["in_progress"] is False
    assert _git(conflicted, "rev-parse", "HEAD") == head
    assert _git(conflicted, "status", "--porcelain") == ""
    assert (conflicted / "README.md").read_text() == "# Demo B\n\nHello\n"


def test_start_refuses_a_dirty_tree_or_a_running_merge(conflicted: Path) -> None:
    (conflicted / "README.md").write_text("uncommitted\n")
    with pytest.raises(BranchResolveError, match="uncommitted changes"):
        branch_resolve(str(conflicted), "start", base="main")
    _git(conflicted, "checkout", "--", "README.md")

    branch_resolve(str(conflicted), "start", base="main")
    with pytest.raises(BranchResolveError, match="already in progress"):
        branch_resolve(str(conflicted), "start", base="main")


def test_binary_conflicts_offer_whole_file_choices(tmp_path: Path) -> None:
    repo = tmp_path / "bin"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "logo.png").write_bytes(b"\x89PNG\0base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "task")
    (repo / "logo.png").write_bytes(b"\x89PNG\0task")
    _git(repo, "commit", "-qam", "task logo")
    _git(repo, "checkout", "-q", "main")
    (repo / "logo.png").write_bytes(b"\x89PNG\0main")
    _git(repo, "commit", "-qam", "main logo")
    _git(repo, "checkout", "-q", "task")

    [entry] = branch_resolve(str(repo), "start", base="main")["conflicts"]
    assert entry["binary"] is True and entry["working"] is None

    branch_resolve(str(repo), "resolve", base="main", path="logo.png", side="ours")
    branch_resolve(str(repo), "complete", base="main")
    assert (repo / "logo.png").read_bytes() == b"\x89PNG\0task"


def test_unknown_action_and_bad_input(conflicted: Path) -> None:
    with pytest.raises(BranchResolveError, match="unknown action"):
        branch_resolve(str(conflicted), "rebase", base="main")
    branch_resolve(str(conflicted), "start", base="main")
    with pytest.raises(BranchResolveError, match="either the resolved content or a side"):
        branch_resolve(str(conflicted), "resolve", base="main", path="README.md")
    with pytest.raises(BranchResolveError, match="side must be"):
        branch_resolve(str(conflicted), "resolve", base="main", path="README.md", side="both")
