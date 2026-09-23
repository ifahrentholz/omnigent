"""Tests for landing a task branch into its base (real git)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from omnigent.runner.branch_merge import BranchMergeError, merge_task_branch

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(cwd: Path, *args: str) -> str:
    """
    Run git in ``cwd`` with a fixed identity.

    :param cwd: Working directory.
    :param args: Git arguments.
    :returns: Stripped stdout.
    """
    return subprocess.run(
        ["git", *args], cwd=cwd, env=_ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """
    Build a main checkout on ``main`` and a task worktree with one commit.

    :param tmp_path: Pytest temporary directory.
    :param monkeypatch: Pytest monkeypatch (git identity for merge commits).
    :returns: ``(main_checkout, task_worktree)``.
    """
    for key, value in _ENV.items():
        if key.startswith("GIT_"):
            monkeypatch.setenv(key, value)
    main = (tmp_path / "repo").resolve()
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    (main / "app.py").write_text("a = 1\n")
    _git(main, "add", ".")
    _git(main, "commit", "-q", "-m", "init")
    worktree = tmp_path / "repo-worktrees" / "omni-task"
    _git(main, "worktree", "add", "-q", "-b", "omni/task", str(worktree))
    (worktree / "feature.py").write_text("done = True\n")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-q", "-m", "task work")
    return main, worktree


def test_merge_keeps_the_task_commits(task: tuple[Path, Path]) -> None:
    """``merge`` lands the branch with a merge commit on the base checkout."""
    main, worktree = task
    result = merge_task_branch(str(worktree), "main")

    assert (result.branch, result.base) == ("omni/task", "main")
    assert Path(result.checkout).resolve() == main
    assert (main / "feature.py").read_text() == "done = True\n"
    assert _git(main, "rev-parse", "HEAD") == result.commit
    assert _git(main, "log", "-1", "--format=%s") == "Merge omni/task into main"
    assert "task work" in _git(main, "log", "--format=%s")


def test_squash_lands_one_commit(task: tuple[Path, Path]) -> None:
    """``squash`` puts the branch's diff on the base as a single commit."""
    main, worktree = task
    merge_task_branch(str(worktree), "main", strategy="squash", message="Add feature")

    assert _git(main, "log", "--format=%s") == "Add feature\ninit"
    assert (main / "feature.py").exists()


def test_conflict_leaves_both_trees_untouched(task: tuple[Path, Path]) -> None:
    """A conflicting merge is aborted and reports the conflicting files."""
    main, worktree = task
    (worktree / "app.py").write_text("a = 2\n")
    _git(worktree, "commit", "-q", "-am", "task edit")
    (main / "app.py").write_text("a = 3\n")
    _git(main, "commit", "-q", "-am", "main edit")
    head_before = _git(main, "rev-parse", "HEAD")

    with pytest.raises(BranchMergeError) as excinfo:
        merge_task_branch(str(worktree), "main")

    assert excinfo.value.conflicts == ["app.py"]
    assert _git(main, "rev-parse", "HEAD") == head_before
    assert _git(main, "status", "--porcelain") == ""
    assert (main / "app.py").read_text() == "a = 3\n"


def test_uncommitted_task_work_is_refused(task: tuple[Path, Path]) -> None:
    """Untracked or modified files in the task would be left behind."""
    _, worktree = task
    (worktree / "scratch.txt").write_text("wip\n")
    with pytest.raises(BranchMergeError, match="task worktree has uncommitted changes"):
        merge_task_branch(str(worktree), "main")


def test_modified_base_checkout_is_refused(task: tuple[Path, Path]) -> None:
    """Someone's edits in the base checkout are never mixed into a merge."""
    main, worktree = task
    (main / "app.py").write_text("a = 99\n")
    with pytest.raises(BranchMergeError, match="'main' checkout .* has uncommitted changes"):
        merge_task_branch(str(worktree), "main")


def test_untracked_scratch_in_base_does_not_block(task: tuple[Path, Path]) -> None:
    """Untracked tool scratch files in the base checkout do not block landing."""
    main, worktree = task
    (main / "scratch-dir").mkdir()
    (main / "scratch-dir" / "probe").write_text("x")
    merge_task_branch(str(worktree), "main")
    assert (main / "feature.py").exists()


def test_base_must_be_checked_out_somewhere(task: tuple[Path, Path]) -> None:
    """Without a checkout of the base there is nowhere to merge into."""
    _, worktree = task
    with pytest.raises(BranchMergeError, match="no checkout has 'release'"):
        merge_task_branch(str(worktree), "release")


def test_a_briefly_held_index_lock_is_waited_out(task: tuple[Path, Path]) -> None:
    """A concurrent git (the runner's file watcher) holding index.lock only
    delays the merge instead of failing it."""
    import threading

    main, worktree = task
    lock = Path(_git(main, "rev-parse", "--absolute-git-dir")) / "index.lock"
    lock.write_text("")
    release = threading.Timer(0.5, lock.unlink)
    release.start()
    try:
        merge_task_branch(str(worktree), "main")
    finally:
        release.join()
    assert (main / "feature.py").exists()


def test_failed_squash_commit_leaves_the_base_clean(
    task: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the squash commit fails (e.g. no git identity), nothing stays staged."""
    main, worktree = task
    for key in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(main.parent))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    _git(main, "config", "user.useConfigOnly", "true")
    head_before = _git(main, "rev-parse", "HEAD")

    with pytest.raises(BranchMergeError, match="commit"):
        merge_task_branch(str(worktree), "main", strategy="squash")

    assert _git(main, "rev-parse", "HEAD") == head_before
    assert _git(main, "status", "--porcelain", "--untracked-files=no") == ""
