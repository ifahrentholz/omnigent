"""Per-worktree port ranges: allocation, reuse, config and env exposure.

Parallel task worktrees that start dev servers must not collide on ports.
Each linked worktree gets a unique index and a port range, exported to the
processes started inside it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from omnigent.host.git_worktree import WorktreeError, create_worktree
from omnigent.host.worktree_ports import read_worktree_ports, worktree_port_env
from omnigent.host.worktree_setup import load_worktree_setup
from omnigent.inner.datamodel import OSEnvSpec
from omnigent.inner.os_env import _child_shell_env
from omnigent.runner.app import _build_spawn_env_from_spec
from omnigent.runner.branch_diff import branch_changes
from omnigent.spec.types import AgentSpec, ExecutorSpec

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


def _repo(tmp_path: Path, config: str | None = None) -> Path:
    """
    Create a one-commit repo, optionally with a ``.omnigent/worktree.yaml``.

    :param tmp_path: Pytest temporary directory.
    :param config: YAML text of the worktree config, or ``None``.
    :returns: The repository root.
    """
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi\n")
    if config is not None:
        (repo / ".omnigent").mkdir()
        (repo / ".omnigent" / "worktree.yaml").write_text(config)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _worktree(repo: Path, branch: str) -> Path:
    """
    Create a worktree on a new branch.

    :param repo: Repository root.
    :param branch: New branch name.
    :returns: The worktree directory.
    """
    return Path(create_worktree(repo_path=str(repo), branch_name=branch).worktree_path)


def test_parallel_worktrees_get_distinct_ranges_and_reuse_freed_ones(tmp_path: Path) -> None:
    """Indices are unique among live worktrees; a removed one's index is reused."""
    repo = _repo(tmp_path)
    first = _worktree(repo, "omni/a")
    second = _worktree(repo, "omni/b")

    assert worktree_port_env(first) == {
        "OMNIGENT_WORKTREE_INDEX": "1",
        "OMNIGENT_PORT_BASE": "3010",
        "OMNIGENT_PORT_SPAN": "10",
        "PORT": "3010",
    }
    assert worktree_port_env(second)["PORT"] == "3020"

    _git(repo, "worktree", "remove", "--force", str(first))
    third = _worktree(repo, "omni/c")
    assert worktree_port_env(third)["OMNIGENT_WORKTREE_INDEX"] == "1"


def test_env_applies_below_the_worktree_root_only(tmp_path: Path) -> None:
    """Subdirectories inherit the range; the main checkout and other dirs get none."""
    repo = _repo(tmp_path)
    worktree = _worktree(repo, "omni/a")
    (worktree / "web").mkdir()

    assert worktree_port_env(worktree / "web")["PORT"] == "3010"
    assert worktree_port_env(repo) == {}
    assert worktree_port_env(tmp_path) == {}
    assert worktree_port_env(None) == {}


def test_config_sets_base_and_span(tmp_path: Path) -> None:
    """``ports.base`` / ``ports.span`` shape the range."""
    repo = _repo(tmp_path, "ports:\n  base: 4000\n  span: 20\n")
    worktree = _worktree(repo, "omni/a")

    ports = read_worktree_ports(worktree)
    assert ports is not None
    assert (ports.index, ports.base, ports.span) == (1, 4020, 20)


def test_ports_false_turns_allocation_off(tmp_path: Path) -> None:
    """A repo can opt out of port allocation."""
    repo = _repo(tmp_path, "ports: false\n")
    assert worktree_port_env(_worktree(repo, "omni/a")) == {}


def test_setup_command_sees_the_range(tmp_path: Path) -> None:
    """Setup hooks can template local config from the allocated ports."""
    repo = _repo(tmp_path, 'setup: echo "PORT=$PORT" > .env.local\n')
    worktree = _worktree(repo, "omni/a")
    assert (worktree / ".env.local").read_text().strip() == "PORT=3010"


@pytest.mark.parametrize(
    "config",
    ["ports: 3000\n", "ports:\n  base: 0\n", "ports:\n  span: true\n", "ports:\n  base: 70000\n"],
)
def test_invalid_ports_config_is_rejected(tmp_path: Path, config: str) -> None:
    """
    A malformed ``ports`` value fails loud.

    :param tmp_path: Pytest temporary directory.
    :param config: Invalid YAML text.
    """
    (tmp_path / ".omnigent").mkdir()
    (tmp_path / ".omnigent" / "worktree.yaml").write_text(config)
    with pytest.raises(WorktreeError):
        load_worktree_setup(tmp_path)


def test_agent_shell_commands_get_the_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shell tools run in a worktree override an inherited PORT with the worktree's."""
    monkeypatch.setenv("PORT", "8080")
    repo = _repo(tmp_path)
    worktree = _worktree(repo, "omni/a")

    assert _child_shell_env(worktree)["PORT"] == "3010"
    assert _child_shell_env(repo)["PORT"] == "8080"


def test_branch_changes_reports_the_allocation(tmp_path: Path) -> None:
    """The Worktrees board reads the range from the branch-changes payload."""
    repo = _repo(tmp_path)
    worktree = _worktree(repo, "omni/a")

    assert branch_changes(str(worktree), base="main")["ports"] == {
        "index": 1,
        "base": 3010,
        "span": 10,
    }
    assert branch_changes(str(repo), base="main")["ports"] is None


def test_harness_spawn_env_carries_the_range(tmp_path: Path) -> None:
    """SDK harnesses run their tools inside the worktree, so their env has the range."""
    repo = _repo(tmp_path)
    worktree = _worktree(repo, "omni/a")
    spec = AgentSpec(
        spec_version=1,
        name="ports-test",
        executor=ExecutorSpec(type="omnigent", config={"harness": "goose"}),
        os_env=OSEnvSpec(type="caller_process"),
    )

    env = _build_spawn_env_from_spec(spec, "goose", cwd=worktree)
    assert env is not None
    assert env["PORT"] == "3010"
    assert "PORT" not in (_build_spawn_env_from_spec(spec, "goose", cwd=repo) or {})
