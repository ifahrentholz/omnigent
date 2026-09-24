"""Conflict prediction for parallel task branches via ``git merge-tree``.

Two tasks touching the same file may still merge cleanly (different hunks)
or may really conflict. The dry-run tells them apart without touching any
worktree, against the base and against sibling task branches.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnigent.runner.branch_diff import BranchDiffError, branch_conflicts


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


def _commit_on(repo: Path, branch: str, files: dict[str, str]) -> None:
    """
    Create ``branch`` from ``main`` with one commit writing ``files``.

    :param repo: Repository root.
    :param branch: New branch name.
    :param files: Relative path to content.
    """
    _git(repo, "checkout", "-q", "-b", branch, "main")
    for path, content in files.items():
        (repo / path).write_text(content)
    _git(repo, "commit", "-q", "-am", f"{branch} work")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """
    Build ``main`` plus three task branches.

    ``omni/a`` and ``omni/b`` edit the same line of ``app.py`` (a real
    conflict); ``omni/c`` edits a different line of it (overlap, but clean).

    :param tmp_path: Pytest temporary directory.
    :returns: The repository, checked out on ``omni/a``.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "app.py").write_text("one = 1\ntwo = 2\nthree = 3\nfour = 4\nfive = 5\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    _commit_on(root, "omni/b", {"app.py": "one = 'b'\ntwo = 2\nthree = 3\nfour = 4\nfive = 5\n"})
    _commit_on(root, "omni/c", {"app.py": "one = 1\ntwo = 2\nthree = 3\nfour = 4\nfive = 'c'\n"})
    _commit_on(root, "omni/a", {"app.py": "one = 'a'\ntwo = 2\nthree = 3\nfour = 4\nfive = 5\n"})
    return root


def _by_ref(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    """
    Index the results by ref.

    :param payload: ``branch_conflicts`` result.
    :returns: ``{ref: result}``.
    """
    results = payload["results"]
    assert isinstance(results, list)
    return {str(item["ref"]): item for item in results}


def test_same_file_is_a_conflict_only_when_hunks_collide(repo: Path) -> None:
    """``omni/b`` edits the same line (conflict); ``omni/c`` a different one (clean)."""
    payload = branch_conflicts(str(repo), base="main", against="omni/b,omni/c")

    results = _by_ref(payload)
    assert results["main"] == {"ref": "main", "clean": True, "files": []}
    assert results["omni/b"] == {"ref": "omni/b", "clean": False, "files": ["app.py"]}
    assert results["omni/c"] == {"ref": "omni/c", "clean": True, "files": []}
    assert payload["supported"] is True
    assert payload["dirty"] is False


def test_landing_a_sibling_turns_into_a_base_conflict(repo: Path) -> None:
    """Once ``omni/b`` landed on ``main``, ``omni/a`` conflicts with its base."""
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-edit", "omni/b")
    _git(repo, "checkout", "-q", "omni/a")

    results = _by_ref(branch_conflicts(str(repo), base="main"))
    assert results["main"]["clean"] is False
    assert results["main"]["files"] == ["app.py"]


def test_the_dry_run_changes_nothing(repo: Path) -> None:
    """No ref, index or working-tree file moves."""
    before = (_git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain"))
    branch_conflicts(str(repo), base="main", against="omni/b")
    assert (_git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain")) == before


def test_uncommitted_edits_are_flagged(repo: Path) -> None:
    """The prediction covers commits only, so dirty work is surfaced."""
    (repo / "app.py").write_text("changed\n")
    assert branch_conflicts(str(repo), base="main")["dirty"] is True


def test_unknown_branch_is_reported_without_failing(repo: Path) -> None:
    """A sibling branch that vanished has no verdict."""
    results = _by_ref(branch_conflicts(str(repo), base="main", against="omni/gone"))
    assert results["omni/gone"] == {"ref": "omni/gone", "clean": None, "files": []}


def test_non_git_workspace_is_rejected(tmp_path: Path) -> None:
    """Plain directories have nothing to predict."""
    with pytest.raises(BranchDiffError):
        branch_conflicts(str(tmp_path), base="main")
