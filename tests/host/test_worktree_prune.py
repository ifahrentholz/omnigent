"""``omnigent worktrees prune``: leftover sub-agent worktrees, safely removed.

Worktrees of live sessions are never touched, dirty ones need ``--force``,
and branches survive unless their work already landed and the user asked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

import omnigent.cli as cli_module
from omnigent.host.worktree_prune import find_prune_candidates, prune_worktree


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
def repo(tmp_path: Path) -> Path:
    """
    Build a repo with four sub-agent worktrees and one user worktree.

    - ``omni/live``: its session is live.
    - ``omni/archived``: its session is archived; has an unlanded commit.
    - ``omni/orphan``: no session; its commit was squash-landed on ``main``.
    - ``omni/dirty``: no session; has an uncommitted edit.
    - ``feature/mine``: a user worktree, never a candidate.

    :param tmp_path: Pytest temporary directory.
    :returns: The main checkout.
    """
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "app.py").write_text("x = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    for branch in ("omni/live", "omni/archived", "omni/orphan", "omni/dirty", "feature/mine"):
        _git(root, "worktree", "add", "-q", "-b", branch, str(tmp_path / branch.replace("/", "-")))
    archived = tmp_path / "omni-archived"
    (archived / "wip.py").write_text("todo = True\n")
    _git(archived, "add", ".")
    _git(archived, "commit", "-q", "-m", "wip")
    orphan = tmp_path / "omni-orphan"
    (orphan / "done.py").write_text("done = True\n")
    _git(orphan, "add", ".")
    _git(orphan, "commit", "-q", "-m", "done")
    _git(root, "merge", "-q", "--squash", "omni/orphan")
    _git(root, "commit", "-q", "-m", "land orphan")
    (tmp_path / "omni-dirty" / "app.py").write_text("x = 2\n")
    return root


def _sessions(tmp_path: Path) -> list[dict[str, Any]]:
    """
    Server view: a live and an archived session.

    :param tmp_path: Pytest temporary directory.
    :returns: Session list items.
    """
    return [
        {"id": "conv_live", "workspace": str(tmp_path / "omni-live"), "archived": False},
        {
            "id": "conv_old",
            "workspace": str(tmp_path / "omni-archived"),
            "archived": True,
            "git_base_branch": "main",
        },
    ]


def test_lists_archived_and_orphaned_sub_agent_worktrees(repo: Path, tmp_path: Path) -> None:
    """Live sessions and non-``omni/`` worktrees are left out."""
    candidates = {c.branch: c for c in find_prune_candidates(str(repo), _sessions(tmp_path))}

    assert set(candidates) == {"omni/archived", "omni/orphan", "omni/dirty"}
    assert candidates["omni/archived"].reason == "session archived"
    assert candidates["omni/archived"].session_id == "conv_old"
    assert candidates["omni/archived"].landed is False
    assert candidates["omni/orphan"].reason == "no session"
    assert candidates["omni/orphan"].landed is True
    assert candidates["omni/dirty"].dirty is True


def test_prune_keeps_unlanded_branches_and_refuses_dirty_worktrees(
    repo: Path, tmp_path: Path
) -> None:
    """Branch deletion only for landed work; a dirty worktree needs ``force``."""
    from omnigent.host.git_worktree import WorktreeError

    candidates = {c.branch: c for c in find_prune_candidates(str(repo), _sessions(tmp_path))}

    assert prune_worktree(candidates["omni/archived"], delete_branch=True) is False
    assert prune_worktree(candidates["omni/orphan"], delete_branch=True) is True
    with pytest.raises(WorktreeError):
        prune_worktree(candidates["omni/dirty"])

    branches = _git(repo, "branch", "--list", "--format=%(refname:short)").splitlines()
    assert "omni/archived" in branches
    assert "omni/orphan" not in branches
    assert not (tmp_path / "omni-archived").exists()
    assert (tmp_path / "omni-dirty" / "app.py").read_text() == "x = 2\n"


def test_cli_confirms_then_removes(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command lists candidates, asks, and skips dirty worktrees."""
    monkeypatch.setattr(cli_module, "_fetch_all_sessions", lambda _url: _sessions(tmp_path))
    runner = CliRunner()

    dry = runner.invoke(
        cli_module.cli,
        ["worktrees", "prune", "--repo", str(repo), "--server", "http://x", "--dry-run"],
    )
    assert dry.exit_code == 0, dry.output
    assert "omni/dirty" in dry.output and "skipped" in dry.output
    assert (tmp_path / "omni-orphan").exists()

    declined = runner.invoke(
        cli_module.cli,
        ["worktrees", "prune", "--repo", str(repo), "--server", "http://x"],
        input="n\n",
    )
    assert declined.exit_code == 0, declined.output
    assert (tmp_path / "omni-orphan").exists()

    done = runner.invoke(
        cli_module.cli,
        ["worktrees", "prune", "--repo", str(repo), "--server", "http://x", "--yes"],
    )
    assert done.exit_code == 0, done.output
    assert "Removed" in done.output
    assert not (tmp_path / "omni-orphan").exists()
    assert not (tmp_path / "omni-archived").exists()
    assert (tmp_path / "omni-dirty").exists()
    assert (tmp_path / "omni-live").exists()


def test_cli_refuses_without_the_session_list(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the server a worktree could belong to a live session, so nothing is removed."""

    def _down(_url: str) -> list[dict[str, Any]]:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(cli_module, "_fetch_all_sessions", _down)
    result = CliRunner().invoke(
        cli_module.cli,
        ["worktrees", "prune", "--repo", str(repo), "--server", "http://x", "--yes"],
    )
    assert result.exit_code != 0
    assert "cannot list sessions" in result.output
    assert (tmp_path / "omni-orphan").exists()
