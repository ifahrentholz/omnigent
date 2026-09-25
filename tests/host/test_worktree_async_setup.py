"""Tests for ``setup_async``: a worktree setup that runs in the background.

The host starts the command detached after creating the worktree and returns
at once; the runner holds the worker's turn until it finished and fails one
turn when it did not succeed.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from omnigent.host import worktree_async_setup as async_setup
from omnigent.host.git_worktree import WorktreeError, create_worktree
from omnigent.host.worktree_async_setup import (
    MAX_ASYNC_SETUP_TIMEOUT_S,
    WorktreeSetupFailedError,
    await_worktree_setup,
    read_async_setup,
)
from omnigent.host.worktree_ports import worktree_admin_dir
from omnigent.host.worktree_setup import load_worktree_setup

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, env={**os.environ, **_GIT_ENV}, check=True, capture_output=True
    )


def _repo(tmp_path: Path, config: str) -> Path:
    """
    Create a repo whose committed ``.omnigent/worktree.yaml`` is ``config``.

    :param tmp_path: Pytest temporary directory.
    :param config: YAML text of the worktree config.
    :returns: The repository root.
    """
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".omnigent").mkdir()
    (repo / ".omnigent" / "worktree.yaml").write_text(config)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _wait_done(worktree: Path, timeout: float = 20.0) -> async_setup.AsyncSetupStatus:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = read_async_setup(worktree)
        assert status is not None
        if status.state != "running":
            return status
        time.sleep(0.1)
    raise AssertionError("background setup did not finish")


def test_create_returns_while_the_background_setup_runs(tmp_path: Path) -> None:
    """The worktree is handed out at once; the setup finishes later in it."""
    repo = _repo(
        tmp_path,
        'setup_async: sleep 1 && echo "port=$PORT" > .installed && echo installing\n',
    )

    started = time.monotonic()
    worktree = Path(create_worktree(repo_path=str(repo), branch_name="omni/a").worktree_path)

    assert time.monotonic() - started < 1.0
    status = read_async_setup(worktree)
    assert status is not None and status.state == "running"
    done = _wait_done(worktree)
    assert (done.state, done.exit_code) == ("ok", 0)
    assert (worktree / ".installed").read_text().strip() == "port=3010"
    assert "installing" in async_setup.log_path(worktree).read_text()  # type: ignore[union-attr]


def test_a_failing_setup_records_the_exit_code_and_output(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "setup_async: echo broken lockfile >&2; exit 3\n")
    worktree = Path(create_worktree(repo_path=str(repo), branch_name="omni/b").worktree_path)

    done = _wait_done(worktree)

    assert (done.state, done.exit_code) == ("failed", 3)
    assert done.error is not None and "broken lockfile" in done.error
    assert worktree.is_dir(), "a failed background setup keeps the worktree for inspection"


def test_the_job_kills_a_setup_that_exceeds_its_timeout(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "setup_async: sleep 30\nsetup_async_timeout: 1\n")
    worktree = Path(create_worktree(repo_path=str(repo), branch_name="omni/c").worktree_path)

    done = _wait_done(worktree)

    assert done.state == "failed"
    assert done.error == "timed out after 1s"


def test_config_validation(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "setup_async: make deps\nsetup_async_timeout: 999999\n")
    config = load_worktree_setup(repo)
    assert config is not None
    assert config.setup_async == "make deps"
    assert config.setup_async_timeout == MAX_ASYNC_SETUP_TIMEOUT_S

    (repo / ".omnigent" / "worktree.yaml").write_text("setup_async: '  '\n")
    with pytest.raises(WorktreeError, match="setup_async"):
        load_worktree_setup(repo)
    (repo / ".omnigent" / "worktree.yaml").write_text("setup_async: x\nsetup_async_timeout: 0\n")
    with pytest.raises(WorktreeError, match="setup_async_timeout"):
        load_worktree_setup(repo)


def _record(worktree: Path, status: async_setup.AsyncSetupStatus) -> None:
    admin = worktree_admin_dir(worktree)
    assert admin is not None
    (admin / async_setup.STATUS_FILE).write_text(json.dumps(asdict(status)))


def _worktree_without_setup(tmp_path: Path) -> Path:
    repo = _repo(tmp_path, "copy: []\n")
    return Path(create_worktree(repo_path=str(repo), branch_name="omni/d").worktree_path)


def test_a_running_record_whose_job_died_reads_as_failed(tmp_path: Path) -> None:
    worktree = _worktree_without_setup(tmp_path)
    dead = subprocess.Popen(["true"])
    dead.wait()
    base = async_setup.AsyncSetupStatus(
        state="running", command="x", timeout=60, started_at=time.time()
    )

    _record(worktree, replace(base, pid=dead.pid))
    assert read_async_setup(worktree).state == "failed"  # type: ignore[union-attr]

    _record(worktree, replace(base, started_at=time.time() - 3600))
    assert read_async_setup(worktree).error == "setup process never started"  # type: ignore[union-attr]


def test_a_worktree_without_background_setup_does_not_hold_turns(tmp_path: Path) -> None:
    worktree = _worktree_without_setup(tmp_path)
    assert read_async_setup(worktree) is None
    asyncio.run(await_worktree_setup(worktree))
    asyncio.run(await_worktree_setup(None))


def test_the_gate_waits_for_the_setup_then_fails_one_turn(tmp_path: Path) -> None:
    """A turn waits while it runs; the first turn after a failure fails, later ones pass."""
    repo = _repo(tmp_path, "setup_async: sleep 1; exit 2\n")
    worktree = Path(create_worktree(repo_path=str(repo), branch_name="omni/e").worktree_path)

    started = time.monotonic()
    with pytest.raises(WorktreeSetupFailedError, match="exit 2"):
        asyncio.run(await_worktree_setup(worktree, poll_s=0.1))
    assert time.monotonic() - started >= 0.9, "the turn was held while the setup ran"

    asyncio.run(await_worktree_setup(worktree, poll_s=0.1))
    assert read_async_setup(worktree).reported is True  # type: ignore[union-attr]


def test_the_gate_passes_after_a_successful_setup(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "setup_async: sleep 0.5\n")
    worktree = Path(create_worktree(repo_path=str(repo), branch_name="omni/f").worktree_path)

    asyncio.run(await_worktree_setup(worktree, poll_s=0.1))

    assert read_async_setup(worktree).state == "ok"  # type: ignore[union-attr]
