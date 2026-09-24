"""Per-worktree port ranges so parallel tasks don't collide on dev servers.

Every linked worktree gets a small index, unique among the repo's live
worktrees, and a port range derived from it (``base + span * index``). The
allocation lives in the worktree's git admin directory
(``.git/worktrees/<name>/``), so ``git worktree remove`` and ``prune`` free
it with the worktree. Terminals, shell tools and harness processes started
inside the worktree see it as environment variables (:func:`worktree_port_env`).

The range is configurable in ``.omnigent/worktree.yaml``::

    ports:
      base: 4000   # default 3000
      span: 20     # default 10

``ports: false`` turns allocation off for the repo.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ALLOCATION_FILE = "omnigent-ports.json"
_LOCK_FILE = "omnigent-ports.lock"
DEFAULT_PORT_BASE = 3000
DEFAULT_PORT_SPAN = 10
_MAX_PORT = 65535


@dataclass(frozen=True)
class WorktreePorts:
    """
    A worktree's allocated port range.

    :param index: Index unique among the repo's live worktrees, starting at 1.
    :param base: First port of the range, e.g. ``3010``.
    :param span: Number of ports in the range, e.g. ``10``.
    """

    index: int
    base: int
    span: int

    def env(self) -> dict[str, str]:
        """
        Environment variables advertising this range.

        :returns: ``OMNIGENT_WORKTREE_INDEX``, ``OMNIGENT_PORT_BASE``,
            ``OMNIGENT_PORT_SPAN`` and ``PORT`` (the first port).
        """
        return {
            "OMNIGENT_WORKTREE_INDEX": str(self.index),
            "OMNIGENT_PORT_BASE": str(self.base),
            "OMNIGENT_PORT_SPAN": str(self.span),
            "PORT": str(self.base),
        }

    def to_json(self) -> dict[str, int]:
        """
        Serialize for the allocation file and API payloads.

        :returns: ``{"index", "base", "span"}``.
        """
        return {"index": self.index, "base": self.base, "span": self.span}


def _admin_dir(worktree: Path) -> Path | None:
    """
    Return a linked worktree's git admin directory.

    :param worktree: The worktree's top-level directory.
    :returns: ``<common>/worktrees/<name>``, or ``None`` for a main checkout
        or a non-git directory.
    """
    dotgit = worktree / ".git"
    if not dotgit.is_file():
        return None
    try:
        first = dotgit.read_text().splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first.startswith("gitdir:"):
        return None
    admin = Path(first.removeprefix("gitdir:").strip())
    if not admin.is_absolute():
        admin = worktree / admin
    return admin if admin.is_dir() else None


def _common_dir(admin: Path) -> Path:
    """
    Return the repository's shared git directory for a worktree admin dir.

    :param admin: ``<common>/worktrees/<name>``.
    :returns: The common git directory.
    """
    try:
        pointer = (admin / "commondir").read_text().strip()
    except OSError:
        return admin.parent.parent
    common = Path(pointer)
    return (common if common.is_absolute() else admin / common).resolve()


def _read_allocation(admin: Path) -> WorktreePorts | None:
    """
    Read an admin dir's allocation file.

    :param admin: A worktree admin directory.
    :returns: The allocation, or ``None`` when absent or malformed.
    """
    try:
        raw = json.loads((admin / ALLOCATION_FILE).read_text())
        return WorktreePorts(index=int(raw["index"]), base=int(raw["base"]), span=int(raw["span"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


@contextlib.contextmanager
def _locked(common: Path) -> Iterator[None]:
    """
    Serialize allocations for one repository.

    Uses ``flock`` where available; elsewhere allocations are unlocked.

    :param common: The repository's common git directory.
    """
    try:
        import fcntl
    except ImportError:
        yield
        return
    with open(common / _LOCK_FILE, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def allocate_worktree_ports(
    worktree: Path,
    *,
    base: int = DEFAULT_PORT_BASE,
    span: int = DEFAULT_PORT_SPAN,
) -> WorktreePorts | None:
    """
    Give a new linked worktree the smallest free index and its port range.

    :param worktree: The new worktree's directory.
    :param base: Port the index-0 range would start at; index 1 starts at
        ``base + span`` so the main checkout keeps ``base``.
    :param span: Ports per worktree.
    :returns: The allocation (an existing one is kept), or ``None`` when the
        directory is not a linked worktree or no range fits below 65536.
    """
    admin = _admin_dir(worktree)
    if admin is None:
        return None
    existing = _read_allocation(admin)
    if existing is not None:
        return existing
    common = _common_dir(admin)
    with _locked(common):
        used = {
            allocation.index
            for sibling in (common / "worktrees").iterdir()
            if sibling.is_dir() and (allocation := _read_allocation(sibling)) is not None
        }
        index = 1
        while index in used:
            index += 1
        ports = WorktreePorts(index=index, base=base + span * index, span=span)
        if ports.base + span - 1 > _MAX_PORT:
            logger.warning("no port range left for worktree %s (index %d)", worktree, index)
            return None
        (admin / ALLOCATION_FILE).write_text(json.dumps(ports.to_json()))
    return ports


def _worktree_root(path: Path) -> Path | None:
    """
    Find the top-level directory of the linked worktree containing ``path``.

    :param path: Any directory, e.g. a terminal's cwd.
    :returns: The worktree root, or ``None`` when ``path`` is not inside one.
    """
    for candidate in (path, *path.parents):
        dotgit = candidate / ".git"
        if dotgit.is_file():
            return candidate
        if dotgit.is_dir():
            return None
    return None


def read_worktree_ports(path: str | Path | None) -> WorktreePorts | None:
    """
    Return the port allocation of the worktree containing ``path``.

    :param path: A directory inside a worktree, e.g. a session workspace.
    :returns: The allocation, or ``None`` outside an allocated worktree.
    """
    if not path:
        return None
    try:
        root = _worktree_root(Path(path).expanduser().resolve())
        admin = _admin_dir(root) if root is not None else None
    except OSError:
        return None
    return _read_allocation(admin) if admin is not None else None


def worktree_port_env(path: str | Path | None) -> dict[str, str]:
    """
    Environment variables for a process started in ``path``.

    :param path: The process's working directory.
    :returns: The worktree's port variables, or ``{}`` outside one.
    """
    ports = read_worktree_ports(path)
    return ports.env() if ports is not None else {}
