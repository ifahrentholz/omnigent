"""Parse-only guard for the foreman example (parallel tasks, one worktree each).

foreman's value is that every dispatch lands in its own git worktree and that
the fleet is bounded. A spec edit that drops either would still run, but the
workers would edit one shared checkout or grow without limit.
"""

from __future__ import annotations

from pathlib import Path

from omnigent.spec.parser import parse
from omnigent.spec.validator import validate

_FOREMAN_DIR = Path(__file__).resolve().parents[2] / "examples" / "foreman"


def test_foreman_workers_run_in_worktrees_and_the_fleet_is_capped() -> None:
    """Every declared worker opts into worktrees and the orchestrator is capped."""
    spec = parse(_FOREMAN_DIR)

    assert spec.async_enabled is True
    assert spec.max_running_subagents == 4
    assert spec.tools.agents == ["claude_code", "codex"]
    workers = {sub.name: sub for sub in spec.sub_agents}
    assert set(workers) == {"claude_code", "codex"}
    for name, worker in workers.items():
        assert worker.worktree is True, f"{name} must isolate each task in a worktree"
        assert worker.os_env is not None, f"{name} needs a filesystem to work in"


def test_foreman_spec_validates() -> None:
    """The example passes the same validation a bundle upload runs."""
    assert validate(parse(_FOREMAN_DIR)).errors == []
