"""Background worktree setup that outlives the host's create-worktree budget.

``setup_async`` in ``.omnigent/worktree.yaml`` names a command, e.g. a cold
``pnpm install``, that may run far longer than the synchronous ``setup``. The
host starts it detached right after creating the worktree. Its state lives
next to the port allocation, in the worktree's git admin directory:

- ``omnigent-setup.json``: ``{"state": "running" | "ok" | "failed", ...}``
- ``omnigent-setup.log``: the command's combined output

The runner holds a session's turn while the state is ``running`` and fails
the first turn after a failure, so the orchestrator hears about it.

Run as ``python -m omnigent.host.worktree_async_setup <admin-dir>``, this
module is the detached job itself: the host's orphan reaper may collect the
job's exit status, so the job records its own result.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from omnigent.host.git_worktree import WorktreeError
from omnigent.host.worktree_ports import worktree_admin_dir

STATUS_FILE = "omnigent-setup.json"
LOG_FILE = "omnigent-setup.log"
DEFAULT_ASYNC_SETUP_TIMEOUT_S = 30 * 60.0
MAX_ASYNC_SETUP_TIMEOUT_S = 4 * 60 * 60.0
# A job that has not recorded its pid by then never started.
_START_GRACE_S = 60.0
# Extra wait past the job's own timeout before the runner gives up on it.
_WAIT_SLACK_S = 120.0
_ERROR_TAIL_CHARS = 2000

SetupState = Literal["running", "ok", "failed"]


class WorktreeSetupFailedError(RuntimeError):
    """A worktree's background setup failed; raised once per failure."""


@dataclass(frozen=True)
class AsyncSetupStatus:
    """
    Recorded state of a worktree's background setup.

    :param state: ``"running"``, ``"ok"`` or ``"failed"``.
    :param command: The ``setup_async`` shell command.
    :param timeout: Seconds before the job kills the command.
    :param started_at: Epoch seconds the host started the job.
    :param pid: The job's pid once it runs, else ``None``.
    :param exit_code: The command's exit status once finished.
    :param error: Why it failed, with the end of the output.
    :param finished_at: Epoch seconds the job finished.
    :param reported: A turn already failed because of this failure.
    """

    state: SetupState
    command: str
    timeout: float
    started_at: float
    pid: int | None = None
    exit_code: int | None = None
    error: str | None = None
    finished_at: float | None = None
    reported: bool = False

    def to_json(self, log_path: Path | None = None) -> dict[str, Any]:
        """
        Serialize for the branch-changes API.

        :param log_path: Where the output is logged, if known.
        :returns: ``{"state", "command", "exit_code", "error", "log"}``.
        """
        return {
            "state": self.state,
            "command": self.command,
            "exit_code": self.exit_code,
            "error": self.error,
            "log": str(log_path) if log_path is not None else None,
        }


def _write(admin: Path, status: AsyncSetupStatus) -> None:
    """
    Atomically replace the status file.

    :param admin: The worktree's git admin directory.
    :param status: The state to record.
    """
    target = admin / STATUS_FILE
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(asdict(status)))
    os.replace(tmp, target)


def _read(admin: Path) -> AsyncSetupStatus | None:
    """
    Load the status file as written.

    :param admin: The worktree's git admin directory.
    :returns: The recorded status, or ``None`` when absent or unreadable.
    """
    try:
        raw = json.loads((admin / STATUS_FILE).read_text())
        return AsyncSetupStatus(**raw)
    except (OSError, ValueError, TypeError):
        return None


def _alive(pid: int) -> bool:
    """
    Report whether a process exists.

    :param pid: Process id.
    :returns: ``False`` only when no such process exists.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tail(text: str) -> str:
    """
    Keep the end of the output for an error message.

    :param text: Full output.
    :returns: At most the last ``_ERROR_TAIL_CHARS`` characters.
    """
    text = text.strip()
    return text if len(text) <= _ERROR_TAIL_CHARS else "…" + text[-_ERROR_TAIL_CHARS:]


def start_async_setup(
    worktree: Path,
    command: str,
    timeout: float,
    env: dict[str, str],
) -> AsyncSetupStatus:
    """
    Start ``command`` detached in ``worktree`` and record it as running.

    :param worktree: The new worktree directory.
    :param command: Shell command to run.
    :param timeout: Seconds before the job kills the command.
    :param env: Environment for the command.
    :returns: The recorded status: ``running``, or ``failed`` when the job
        could not be spawned.
    :raises WorktreeError: When ``worktree`` is not a linked git worktree.
    """
    admin = worktree_admin_dir(worktree)
    if admin is None:
        raise WorktreeError(f"setup_async needs a linked git worktree, got {worktree}")
    status = AsyncSetupStatus(
        state="running", command=command, timeout=timeout, started_at=time.time()
    )
    _write(admin, status)
    try:
        subprocess.Popen(
            [sys.executable, "-m", __name__, str(admin)],
            cwd=worktree,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        status = replace(
            status, state="failed", error=f"could not start: {exc}", finished_at=time.time()
        )
        _write(admin, status)
    return status


def read_async_setup(path: str | Path | None) -> AsyncSetupStatus | None:
    """
    Return the background setup state of the worktree containing ``path``.

    A ``running`` record whose job is gone reads as ``failed``.

    :param path: A directory inside a worktree, e.g. a session workspace.
    :returns: The status, or ``None`` when the worktree has no background setup.
    """
    admin = worktree_admin_dir(path)
    status = _read(admin) if admin is not None else None
    if status is None or status.state != "running":
        return status
    if status.pid is not None and not _alive(status.pid):
        return replace(status, state="failed", error="setup process ended without a result")
    if status.pid is None and time.time() - status.started_at > _START_GRACE_S:
        return replace(status, state="failed", error="setup process never started")
    return status


def log_path(path: str | Path | None) -> Path | None:
    """
    Return where the worktree's background setup logs its output.

    :param path: A directory inside a worktree.
    :returns: The log file path, or ``None`` outside a linked worktree.
    """
    admin = worktree_admin_dir(path)
    return admin / LOG_FILE if admin is not None else None


def _mark_reported(path: str | Path | None, status: AsyncSetupStatus) -> None:
    """
    Record that a turn already failed because of ``status``.

    :param path: A directory inside the worktree.
    :param status: The failure being reported.
    """
    admin = worktree_admin_dir(path)
    if admin is not None:
        _write(admin, replace(status, reported=True))


async def await_worktree_setup(path: str | Path | None, *, poll_s: float = 1.0) -> None:
    """
    Hold a turn until the worktree's background setup finished.

    :param path: The session's working directory.
    :param poll_s: Seconds between status checks.
    :raises WorktreeSetupFailedError: The first time after the setup failed,
        timed out, or its job died. Later turns proceed.
    """
    status = await asyncio.to_thread(read_async_setup, path)
    while status is not None and status.state == "running":
        if time.time() > status.started_at + status.timeout + _WAIT_SLACK_S:
            status = replace(status, state="failed", error="setup did not finish in time")
            break
        await asyncio.sleep(poll_s)
        status = await asyncio.to_thread(read_async_setup, path)
    if status is None or status.state != "failed" or status.reported:
        return
    await asyncio.to_thread(_mark_reported, path, status)
    where = log_path(path)
    raise WorktreeSetupFailedError(
        f"worktree setup {status.command!r} failed: {status.error}"
        + (f" (log: {where})" if where is not None else "")
    )


def _run_job(admin: Path) -> int:
    """
    Run the recorded command and record its result.

    :param admin: The worktree's git admin directory.
    :returns: The process exit status for the job.
    """
    status = _read(admin)
    if status is None or status.state != "running":
        return 1
    status = replace(status, pid=os.getpid())
    _write(admin, status)
    log = admin / LOG_FILE
    with log.open("w") as out:
        proc = subprocess.Popen(
            status.command,
            shell=True,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code = proc.wait(timeout=status.timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            _write(
                admin,
                replace(
                    status,
                    state="failed",
                    error=f"timed out after {status.timeout:.0f}s",
                    finished_at=time.time(),
                ),
            )
            return 1
    if code == 0:
        _write(admin, replace(status, state="ok", exit_code=0, finished_at=time.time()))
        return 0
    output = log.read_text(errors="replace") if log.exists() else ""
    _write(
        admin,
        replace(
            status,
            state="failed",
            exit_code=code,
            error=f"exit {code}: {_tail(output)}",
            finished_at=time.time(),
        ),
    )
    return 1


if __name__ == "__main__":
    sys.exit(_run_job(Path(sys.argv[1])))
