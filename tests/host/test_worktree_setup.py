"""Tests for ``.omnigent/worktree.yaml``: preparing a new worktree.

A new worktree lacks git-ignored local files and installed dependencies. The
repo's ``worktree.yaml`` copies the former from the main checkout and runs a
setup command; a failing setup must not leave a half-prepared worktree.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from omnigent.host.git_worktree import WorktreeError, create_worktree, list_worktrees
from omnigent.host.worktree_setup import (
    MAX_SETUP_TIMEOUT_S,
    is_worktree_config_error,
    load_worktree_setup,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> str:
    """
    Run git in ``repo`` with a fixed identity.

    :param repo: Repository directory.
    :param args: Git arguments.
    :returns: Stripped stdout.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo_with_config(tmp_path: Path, config: str) -> Path:
    """
    Create a repo whose committed ``.omnigent/worktree.yaml`` is ``config``.

    ``.env`` and ``config/app.local.json`` exist in the main checkout but are
    git-ignored, like real local secrets.

    :param tmp_path: Pytest temporary directory.
    :param config: YAML text of the worktree config.
    :returns: The repository root.
    """
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".env\nconfig/*.local.json\n")
    (repo / ".omnigent").mkdir()
    (repo / ".omnigent" / "worktree.yaml").write_text(config)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / ".env").write_text("SECRET=1\n")
    (repo / "config").mkdir()
    (repo / "config" / "app.local.json").write_text("{}\n")
    return repo


def test_copies_ignored_files_and_runs_setup(tmp_path: Path) -> None:
    """Listed local files land in the worktree and setup runs inside it."""
    repo = _repo_with_config(
        tmp_path,
        "copy:\n  - .env\n  - config/*.local.json\nsetup: echo ready > .setup-done\n",
    )
    created = create_worktree(repo_path=str(repo), branch_name="omni/task-1")
    worktree = Path(created.worktree_path)

    assert (worktree / ".env").read_text() == "SECRET=1\n"
    assert (worktree / "config" / "app.local.json").exists()
    assert (worktree / ".setup-done").read_text().strip() == "ready"


def test_failed_setup_rolls_back_worktree_and_branch(tmp_path: Path) -> None:
    """A failing setup raises with its output and leaves nothing behind."""
    repo = _repo_with_config(tmp_path, "setup: echo broken dependency >&2; exit 3\n")

    with pytest.raises(WorktreeError, match=r"exit 3.*broken dependency"):
        create_worktree(repo_path=str(repo), branch_name="omni/task-2")

    assert [wt.branch for wt in list_worktrees(repo_path=str(repo))] == ["main"]
    assert "omni/task-2" not in _git(repo, "branch", "--list")


def test_without_config_nothing_happens(tmp_path: Path) -> None:
    """Repos without a worktree.yaml behave exactly as before."""
    repo = (tmp_path / "plain").resolve()
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")

    created = create_worktree(repo_path=str(repo), branch_name="omni/task-3")
    assert sorted(p.name for p in Path(created.worktree_path).iterdir()) == [".git", "README.md"]


@pytest.mark.parametrize(
    "config",
    ["copy: .env\n", "setup: ''\n", "setup_timeout: -1\n", "- not a mapping\n"],
)
def test_invalid_config_is_rejected(tmp_path: Path, config: str) -> None:
    """
    Malformed configs fail loud instead of being half-applied.

    :param tmp_path: Pytest temporary directory.
    :param config: Invalid YAML text.
    """
    (tmp_path / ".omnigent").mkdir()
    (tmp_path / ".omnigent" / "worktree.yaml").write_text(config)
    with pytest.raises(WorktreeError):
        load_worktree_setup(tmp_path)


def test_setup_timeout_is_capped(tmp_path: Path) -> None:
    """The setup must finish within the worktree-create frame budget."""
    (tmp_path / ".omnigent").mkdir()
    (tmp_path / ".omnigent" / "worktree.yaml").write_text("setup: make\nsetup_timeout: 900\n")
    config = load_worktree_setup(tmp_path)
    assert config is not None
    assert config.setup_timeout == MAX_SETUP_TIMEOUT_S


def test_config_and_setup_failures_are_recognized(tmp_path: Path) -> None:
    """The runner tells repo-caused create failures from infra ones by message."""
    repo = _repo_with_config(tmp_path, "setup: [unclosed\n")
    with pytest.raises(WorktreeError) as bad_yaml:
        create_worktree(repo_path=str(repo), branch_name="omni/task-yaml")
    assert is_worktree_config_error(f"worktree creation failed: {bad_yaml.value.message}")

    (repo / ".omnigent" / "worktree.yaml").write_text("setup: exit 4\n")
    _git(repo, "commit", "-qam", "failing setup")
    with pytest.raises(WorktreeError) as failing:
        create_worktree(repo_path=str(repo), branch_name="omni/task-setup")
    assert is_worktree_config_error(failing.value.message)

    assert not is_worktree_config_error("host 'h1' is offline; reconnect the host and try again")
    assert not is_worktree_config_error("a branch named 'omni/x' already exists")
