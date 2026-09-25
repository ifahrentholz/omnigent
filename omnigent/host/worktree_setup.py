"""Prepare a freshly created git worktree from ``.omnigent/worktree.yaml``.

A new worktree has the branch's tracked files only. Git-ignored local state
(``.env`` files, local config) and installed dependencies are missing, so an
agent starting there wastes turns or fails. A repository can declare what a
worktree needs:

```yaml
# .omnigent/worktree.yaml
copy:            # git-ignored files to copy from the main checkout (globs)
  - .env
  - config/*.local.json
setup: cp .env.example .env
setup_timeout: 60  # seconds, capped at 90
setup_async: pnpm install --frozen-lockfile  # may run longer, in the background
setup_async_timeout: 1800  # seconds, capped at 4 hours
ports:           # per-worktree port range, see omnigent.host.worktree_ports
  base: 3000
  span: 10
```

Runs on the host right after ``git worktree add`` and before the session
starts. The whole worktree create must answer the server within its frame
timeout, so ``setup`` is capped. ``setup_async`` starts afterwards in the
background (see :mod:`omnigent.host.worktree_async_setup`); the worker's
first turn waits for it.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from omnigent.host.git_worktree import WorktreeError
from omnigent.host.worktree_async_setup import (
    DEFAULT_ASYNC_SETUP_TIMEOUT_S,
    MAX_ASYNC_SETUP_TIMEOUT_S,
    start_async_setup,
)
from omnigent.host.worktree_ports import (
    DEFAULT_PORT_BASE,
    DEFAULT_PORT_SPAN,
    WorktreePorts,
    allocate_worktree_ports,
)

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(".omnigent") / "worktree.yaml"
MAX_SETUP_TIMEOUT_S = 90.0
_MAX_COPY_BYTES = 50 * 1024 * 1024
_OUTPUT_TAIL_CHARS = 2000


@dataclass(frozen=True)
class WorktreeSetup:
    """
    Parsed ``.omnigent/worktree.yaml``.

    :param copy: Glob patterns, relative to the main checkout, of files to copy.
    :param setup: Shell command run inside the new worktree, or ``None``.
    :param setup_timeout: Seconds before ``setup`` is aborted.
    :param setup_async: Shell command started in the background, or ``None``.
    :param setup_async_timeout: Seconds before ``setup_async`` is killed.
    :param port_base: Base of the per-worktree port ranges, or ``None`` when
        the repo turned port allocation off.
    :param port_span: Ports per worktree.
    """

    copy: list[str] = field(default_factory=list)
    setup: str | None = None
    setup_timeout: float = MAX_SETUP_TIMEOUT_S
    setup_async: str | None = None
    setup_async_timeout: float = DEFAULT_ASYNC_SETUP_TIMEOUT_S
    port_base: int | None = DEFAULT_PORT_BASE
    port_span: int = DEFAULT_PORT_SPAN


def _parse_ports(raw: object) -> tuple[int | None, int]:
    """
    Validate the ``ports`` field.

    :param raw: ``None``, ``False``, or a ``{"base", "span"}`` mapping.
    :returns: ``(base, span)``; ``base`` is ``None`` when allocation is off.
    :raises WorktreeError: On a malformed value.
    """
    if raw is None or raw is True:
        return DEFAULT_PORT_BASE, DEFAULT_PORT_SPAN
    if raw is False:
        return None, DEFAULT_PORT_SPAN
    if not isinstance(raw, dict):
        raise WorktreeError(f"{CONFIG_PATH}: 'ports' must be a mapping or false")
    base = raw.get("base", DEFAULT_PORT_BASE)
    span = raw.get("span", DEFAULT_PORT_SPAN)
    for name, value in (("base", base), ("span", span)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise WorktreeError(f"{CONFIG_PATH}: 'ports.{name}' must be an integer in 1..65535")
    return base, span


def load_worktree_setup(*roots: Path) -> WorktreeSetup | None:
    """
    Read the first ``.omnigent/worktree.yaml`` found under ``roots``.

    :param roots: Directories to look in, e.g. the new worktree, then the
        main checkout.
    :returns: The parsed config, or ``None`` when no root has one.
    :raises WorktreeError: If the file is not valid YAML or has bad fields.
    """
    for root in roots:
        path = root / CONFIG_PATH
        if path.is_file():
            break
    else:
        return None
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise WorktreeError(f"cannot read {CONFIG_PATH}: {exc}") from exc
    if not isinstance(raw, dict):
        raise WorktreeError(f"{CONFIG_PATH} must be a mapping")
    copy = raw.get("copy") or []
    if not isinstance(copy, list) or not all(isinstance(item, str) for item in copy):
        raise WorktreeError(f"{CONFIG_PATH}: 'copy' must be a list of glob strings")
    setup = raw.get("setup")
    if setup is not None and (not isinstance(setup, str) or not setup.strip()):
        raise WorktreeError(f"{CONFIG_PATH}: 'setup' must be a non-empty command string")
    timeout = _positive_seconds(raw, "setup_timeout", MAX_SETUP_TIMEOUT_S)
    setup_async = raw.get("setup_async")
    if setup_async is not None and (not isinstance(setup_async, str) or not setup_async.strip()):
        raise WorktreeError(f"{CONFIG_PATH}: 'setup_async' must be a non-empty command string")
    async_timeout = _positive_seconds(raw, "setup_async_timeout", DEFAULT_ASYNC_SETUP_TIMEOUT_S)
    port_base, port_span = _parse_ports(raw.get("ports"))
    return WorktreeSetup(
        copy=list(copy),
        setup=setup.strip() if isinstance(setup, str) else None,
        setup_timeout=min(timeout, MAX_SETUP_TIMEOUT_S),
        setup_async=setup_async.strip() if isinstance(setup_async, str) else None,
        setup_async_timeout=min(async_timeout, MAX_ASYNC_SETUP_TIMEOUT_S),
        port_base=port_base,
        port_span=port_span,
    )


def is_worktree_config_error(message: str) -> bool:
    """
    Report whether a worktree create failed on the repository's own setup.

    Every config error names :data:`CONFIG_PATH` and every ``setup`` failure
    starts with ``worktree setup``. Retrying the dispatch without a worktree
    does not fix those; it would only run the task in the shared checkout.

    :param message: The create's error message, possibly wrapped by the server.
    :returns: ``True`` for a bad ``worktree.yaml`` or a failing ``setup``.
    """
    return str(CONFIG_PATH) in message or "worktree setup " in message


def _positive_seconds(raw: dict[str, object], key: str, default: float) -> float:
    """
    Validate an optional duration field.

    :param raw: The parsed config mapping.
    :param key: Field name, e.g. ``"setup_timeout"``.
    :param default: Value when the field is absent.
    :returns: The duration in seconds.
    :raises WorktreeError: When the value is not a positive number.
    """
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise WorktreeError(f"{CONFIG_PATH}: {key!r} must be a positive number")
    return float(value)


def _copy_files(source_root: Path, worktree: Path, patterns: list[str]) -> None:
    """
    Copy matching files from the main checkout into the worktree.

    Files outside the checkout, inside ``.git``, larger than the copy cap, or
    already present in the worktree are skipped.

    :param source_root: The main checkout.
    :param worktree: The new worktree.
    :param patterns: Glob patterns relative to ``source_root``.
    """
    root = source_root.resolve()
    for pattern in patterns:
        for match in root.glob(pattern):
            resolved = match.resolve()
            if not resolved.is_file() or not resolved.is_relative_to(root):
                continue
            relative = resolved.relative_to(root)
            if relative.parts and relative.parts[0] == ".git":
                continue
            if resolved.stat().st_size > _MAX_COPY_BYTES:
                continue
            destination = worktree / relative
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(resolved, destination)


def _tail(text: str) -> str:
    """
    Keep the end of a command's output for an error message.

    :param text: Full output.
    :returns: At most the last ``_OUTPUT_TAIL_CHARS`` characters.
    """
    text = text.strip()
    return text if len(text) <= _OUTPUT_TAIL_CHARS else "…" + text[-_OUTPUT_TAIL_CHARS:]


def _allocate_ports(worktree: Path, config: WorktreeSetup) -> WorktreePorts | None:
    """
    Allocate the worktree's port range; a failure never blocks the create.

    :param worktree: The new worktree directory.
    :param config: The repo's config, or the defaults.
    :returns: The allocation, or ``None`` when off or impossible.
    """
    if config.port_base is None:
        return None
    try:
        return allocate_worktree_ports(worktree, base=config.port_base, span=config.port_span)
    except OSError:
        logger.warning("port allocation failed for worktree %s", worktree, exc_info=True)
        return None


def apply_worktree_setup(source_root: Path, worktree: Path) -> WorktreeSetup | None:
    """
    Prepare a new worktree: ports, local files, ``setup``, then ``setup_async``.

    :param source_root: The main checkout the worktree was created from.
    :param worktree: The new worktree directory.
    :returns: The applied config, or ``None`` when the repo declares none.
    :raises WorktreeError: On an invalid config, or a setup command that
        fails or times out (with the end of its output).
    """
    config = load_worktree_setup(worktree, source_root)
    ports = _allocate_ports(worktree, config or WorktreeSetup())
    if config is None:
        return None
    _copy_files(source_root, worktree, config.copy)
    env = {
        **os.environ,
        **(ports.env() if ports is not None else {}),
        "OMNIGENT_WORKTREE": str(worktree),
        "OMNIGENT_WORKTREE_SOURCE": str(source_root),
    }
    if config.setup is not None:
        _run_setup(worktree, config.setup, config.setup_timeout, env)
    if config.setup_async is not None:
        start_async_setup(worktree, config.setup_async, config.setup_async_timeout, env)
    return config


def _run_setup(worktree: Path, command: str, timeout: float, env: dict[str, str]) -> None:
    """
    Run the synchronous ``setup`` command.

    :param worktree: The new worktree directory.
    :param command: The shell command.
    :param timeout: Seconds before it is aborted.
    :param env: Environment for the command.
    :raises WorktreeError: When the command fails or times out.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=worktree,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"worktree setup {command!r} timed out after {timeout:.0f}s") from exc
    if result.returncode != 0:
        raise WorktreeError(
            f"worktree setup {command!r} failed (exit {result.returncode}): "
            f"{_tail(result.stderr or result.stdout)}"
        )
