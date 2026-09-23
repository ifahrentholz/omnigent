"""Per-dispatch git worktree isolation for orchestrator-spawned sub-agents.

A sub-agent created by ``sys_session_send`` / ``sys_session_create`` normally
shares the parent's workspace, so parallel workers edit one checkout. When
isolation is requested, the child's create body is extended with the parent's
``host_id`` / ``workspace`` plus a ``git`` block, and the server's existing
session-create path creates a worktree that becomes the child's workspace.

Two request strengths:

- ``required`` (the caller passed ``worktree: true``): fail loud when the
  parent session cannot host a worktree.
- ``auto`` (the sub-agent spec declares ``worktree: true``): isolate when the
  parent workspace is a git repo on a bound host, otherwise run unisolated.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Literal

import httpx

from omnigent.host.git_worktree import WorktreeError, validate_branch_name

WorktreeMode = Literal["off", "auto", "required"]

BRANCH_PREFIX = "omni/"
_SLUG_INVALID = re.compile(r"[^a-z0-9._-]+")
_SLUG_MAX_LEN = 48
_GIT_PROBE_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class WorktreeArgs:
    """
    Worktree fields from the object form of a dispatch's ``args``.

    :param requested: ``True``/``False`` when the caller set ``worktree``,
        ``None`` when absent (defer to the sub-agent spec).
    :param base_branch: Optional ref the new branch forks from, e.g.
        ``"main"``.
    """

    requested: bool | None = None
    base_branch: str | None = None


def worktree_args_from_dispatch(args: dict[str, object]) -> WorktreeArgs:
    """
    Extract ``worktree`` / ``base_branch`` from ``sys_session_send`` args.

    :param args: Parsed tool arguments, e.g.
        ``{"args": {"input": "fix it", "worktree": True}}``.
    :returns: The parsed worktree request.
    :raises ValueError: If a field is present with the wrong type.
    """
    raw = args.get("args")
    if not isinstance(raw, dict):
        return WorktreeArgs()
    return _parse_worktree_fields(raw.get("worktree"), raw.get("base_branch"))


def worktree_args_from_create(args: dict[str, object]) -> WorktreeArgs:
    """
    Extract top-level ``worktree`` / ``base_branch`` from ``sys_session_create``.

    :param args: Parsed ``sys_session_create`` arguments.
    :returns: The parsed worktree request.
    :raises ValueError: If a field is present with the wrong type.
    """
    return _parse_worktree_fields(args.get("worktree"), args.get("base_branch"))


def _parse_worktree_fields(raw_worktree: object, raw_base: object) -> WorktreeArgs:
    """
    Validate raw ``worktree`` / ``base_branch`` values.

    :param raw_worktree: Raw ``worktree`` value, expected ``bool`` or ``None``.
    :param raw_base: Raw ``base_branch`` value, expected ``str`` or ``None``.
    :returns: The validated request.
    :raises ValueError: On a wrong type, or ``base_branch`` without isolation.
    """
    if raw_worktree is not None and not isinstance(raw_worktree, bool):
        raise ValueError("'worktree' must be a boolean when provided")
    if raw_base is not None and (not isinstance(raw_base, str) or not raw_base.strip()):
        raise ValueError("'base_branch' must be a non-empty string when provided")
    if raw_base is not None and raw_worktree is False:
        raise ValueError("'base_branch' requires 'worktree' to be enabled")
    return WorktreeArgs(
        requested=raw_worktree,
        base_branch=raw_base.strip() if isinstance(raw_base, str) else None,
    )


def resolve_worktree_mode(request: WorktreeArgs, *, spec_default: bool) -> WorktreeMode:
    """
    Combine the per-dispatch request with the sub-agent spec's default.

    An explicit ``worktree`` always wins. A ``base_branch`` alone implies an
    explicit request, since it is meaningless without isolation.

    :param request: The dispatch's worktree fields.
    :param spec_default: The sub-agent spec's ``worktree`` flag.
    :returns: ``"required"``, ``"auto"`` or ``"off"``.
    """
    if request.requested is True or (request.requested is None and request.base_branch):
        return "required"
    if request.requested is False:
        return "off"
    return "auto" if spec_default else "off"


def subagent_branch_name(session_name: str, work_id: str) -> str:
    """
    Derive a unique, ref-safe branch name for one sub-agent dispatch.

    :param session_name: The child's instance label, e.g. ``"claude_code-1"``.
    :param work_id: The dispatch id, e.g. ``"subagent_a1b2c3d4e5f6"``; its
        tail disambiguates equal labels across orchestrator sessions.
    :returns: A branch name such as ``"omni/claude_code-1-d4e5f6"``.
    """
    slug = _SLUG_INVALID.sub("-", session_name.lower()).strip("-.")[:_SLUG_MAX_LEN]
    slug = slug.rstrip("-.") or "task"
    suffix = work_id.rsplit("_", 1)[-1][-6:] or "0"
    return f"{BRANCH_PREFIX}{slug}-{suffix}"


async def _is_git_work_tree(path: str) -> bool:
    """
    Probe whether ``path`` is inside a git work tree on this machine.

    :param path: Absolute directory path, e.g. ``"/Users/alice/repo"``.
    :returns: ``True`` only when ``git rev-parse`` confirms a work tree.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            path,
            "rev-parse",
            "--is-inside-work-tree",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_PROBE_TIMEOUT_S)
    except (OSError, TimeoutError):
        return False
    return proc.returncode == 0 and stdout.strip() == b"true"


async def build_worktree_create_fields(
    *,
    mode: WorktreeMode,
    request: WorktreeArgs,
    server_client: httpx.AsyncClient,
    parent_session_id: str,
    branch_name: str,
) -> dict[str, object] | str | None:
    """
    Build the create-body fields that make the server create a worktree.

    The branch forks from ``request.base_branch`` when given, else from the
    parent's own worktree branch, else from the main checkout's ``HEAD``.
    Uncommitted parent changes are not carried over.

    :param mode: Resolved worktree mode for this dispatch.
    :param request: The dispatch's worktree fields.
    :param server_client: HTTP client pointed at the Omnigent server.
    :param parent_session_id: The orchestrator session id.
    :param branch_name: The new branch, from :func:`subagent_branch_name`.
    :returns: ``{"host_id", "workspace", "git"}`` to merge into the create
        body; ``None`` when isolation is off or skipped in ``auto`` mode; an
        ``"Error: ..."`` string when ``required`` isolation is impossible.
    """
    if mode == "off":
        return None
    try:
        validate_branch_name(branch_name)
    except WorktreeError as exc:
        return f"Error: invalid sub-agent worktree branch {branch_name!r}: {exc.message}"
    try:
        resp = await server_client.get(f"/v1/sessions/{parent_session_id}", timeout=10.0)
    except (httpx.HTTPError, RuntimeError) as exc:
        if mode == "auto":
            return None
        return f"Error: could not read the parent session to create a worktree: {exc}"
    parent = resp.json() if resp.status_code == 200 else None
    if not isinstance(parent, dict):
        if mode == "auto":
            return None
        return f"Error: could not read the parent session to create a worktree: {resp.status_code}"
    host_id = parent.get("host_id")
    workspace = parent.get("workspace")
    if (
        not isinstance(host_id, str)
        or not host_id
        or not isinstance(workspace, str)
        or not workspace
    ):
        if mode == "auto":
            return None
        return (
            "Error: 'worktree' needs a parent session bound to a host with a "
            "workspace; this session has none. Dispatch without 'worktree'."
        )
    # ``auto`` must never turn a non-git workspace into a failed dispatch;
    # ``required`` lets the server report the precise git error instead.
    if mode == "auto" and not await _is_git_work_tree(workspace):
        return None
    base_branch = request.base_branch
    if base_branch is None:
        parent_branch = parent.get("git_branch")
        if isinstance(parent_branch, str) and parent_branch:
            base_branch = parent_branch
    git: dict[str, object] = {"branch_name": branch_name}
    if base_branch is not None:
        git["base_branch"] = base_branch
    return {"host_id": host_id, "workspace": workspace, "git": git}


def worktree_handle_fields(created: dict[str, object]) -> dict[str, object]:
    """
    Pick the worktree facts from a create response for the tool result.

    :param created: The server's ``POST /v1/sessions`` response body.
    :returns: ``{"worktree": {"path", "branch"}}`` when the child got a
        worktree, else ``{}``.
    """
    branch = created.get("git_branch")
    path = created.get("workspace")
    if isinstance(branch, str) and branch and isinstance(path, str) and path:
        return {"worktree": {"path": path, "branch": branch}}
    return {}
