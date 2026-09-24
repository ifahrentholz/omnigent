"""Clean-only worktree removal: the frame field, its capability and git's refusal.

Archiving a sub-agent frees its worktree only when nothing would be lost, so
the host must keep a worktree with local changes, and the server must not
ask hosts that predate the flag (they would force-remove).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from omnigent.host.frames import (
    CAP_CLEAN_WORKTREE_REMOVE,
    HOST_CAPABILITIES,
    HostRemoveWorktreeFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.git_worktree import WorktreeError, remove_worktree


def _git(repo: Path, *args: str) -> None:
    """
    Run git in ``repo`` with a fixed identity.

    :param repo: Repository directory.
    :param args: Git arguments.
    """
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
    )


def test_frame_round_trips_only_if_clean() -> None:
    """The flag survives the wire; frames from older servers default to off."""
    frame = HostRemoveWorktreeFrame(request_id="r1", worktree_path="/w", only_if_clean=True)
    assert decode_host_frame(encode_host_frame(frame)) == frame

    legacy = json.loads(encode_host_frame(frame))
    del legacy["only_if_clean"]
    decoded = decode_host_frame(json.dumps(legacy))
    assert isinstance(decoded, HostRemoveWorktreeFrame)
    assert decoded.only_if_clean is False


def test_this_host_advertises_clean_remove() -> None:
    """The server gates the flag on this capability."""
    assert CAP_CLEAN_WORKTREE_REMOVE in HOST_CAPABILITIES


@pytest.mark.parametrize("change", ["modified", "untracked"])
def test_clean_only_removal_keeps_a_worktree_with_changes(tmp_path: Path, change: str) -> None:
    """
    Without force, git refuses; the worktree and its edits stay.

    :param tmp_path: Pytest temporary directory.
    :param change: Kind of local change left in the worktree.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "omni/x", str(worktree))
    if change == "modified":
        (worktree / "a.txt").write_text("changed\n")
    else:
        (worktree / "new.txt").write_text("new\n")

    with pytest.raises(WorktreeError):
        remove_worktree(worktree_path=str(worktree), force=False)
    assert worktree.is_dir()

    remove_worktree(worktree_path=str(worktree))
    assert not worktree.exists()


def test_clean_only_removal_removes_a_clean_worktree(tmp_path: Path) -> None:
    """A clean worktree goes; its branch stays."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "omni/x", str(worktree))

    remove_worktree(worktree_path=str(worktree), branch="omni/x", force=False)

    assert not worktree.exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "omni/x"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "omni/x" in branches
