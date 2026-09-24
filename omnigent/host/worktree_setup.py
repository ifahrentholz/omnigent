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
setup: pnpm install --frozen-lockfile --prefer-offline
setup_timeout: 60  # seconds, capped at 90
ports:           # per-worktree port range, see omnigent.host.worktree_ports
  base: 3000
  span: 10
```

Runs on the host right after ``git worktree add`` and before the session
starts. The whole worktree create must answer the server within its frame
timeout, so ``setup`` is capped; long cold installs belong in the agent's
first task instead.
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
    :param port_base: Base of the per-worktree port ranges, or ``None`` when
        the repo turned port allocation off.
    :param port_span: Ports per worktree.
    """

    copy: list[str] = field(default_factory=list)
    setup: str | None = None
    setup_timeout: float = MAX_SETUP_TIMEOUT_S
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
    timeout = raw.get("setup_timeout", MAX_SETUP_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise WorktreeError(f"{CONFIG_PATH}: 'setup_timeout' must be a positive number")
    port_base, port_span = _parse_ports(raw.get("ports"))
    return WorktreeSetup(
        copy=list(copy),
        setup=setup.strip() if isinstance(setup, str) else None,
        setup_timeout=min(float(timeout), MAX_SETUP_TIMEOUT_S),
        port_base=port_base,
        port_span=port_span,
    )


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
    Allocate ports, copy local files and run the setup command for a new worktree.

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
    if config.setup is None:
        return config
    env = {
        **os.environ,
        **(ports.env() if ports is not None else {}),
        "OMNIGENT_WORKTREE": str(worktree),
        "OMNIGENT_WORKTREE_SOURCE": str(source_root),
    }
    try:
        result = subprocess.run(
            config.setup,
            shell=True,
            cwd=worktree,
            env=env,
            capture_output=True,
            text=True,
            timeout=config.setup_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(
            f"worktree setup {config.setup!r} timed out after {config.setup_timeout:.0f}s"
        ) from exc
    if result.returncode != 0:
        raise WorktreeError(
            f"worktree setup {config.setup!r} failed (exit {result.returncode}): "
            f"{_tail(result.stderr or result.stdout)}"
        )
    return config
