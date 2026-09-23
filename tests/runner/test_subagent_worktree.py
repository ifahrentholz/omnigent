"""Tests for per-dispatch git worktree isolation of sub-agents.

``sys_session_send`` / ``sys_session_create`` can ask the server to create a
git worktree for a new child session. The runner copies the parent's
``host_id`` / ``workspace`` into the create body plus a ``git`` block; the
server's existing session-create path does the actual ``git worktree add``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.host.git_worktree import validate_branch_name
from omnigent.runner.subagent_worktree import (
    WorktreeArgs,
    build_worktree_create_fields,
    resolve_worktree_mode,
    subagent_branch_name,
    worktree_args_from_create,
    worktree_args_from_dispatch,
    worktree_handle_fields,
)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """
    Create an initialized git repository.

    :param tmp_path: Pytest temporary directory.
    :returns: Path of the repository root.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _server(
    parent: dict[str, Any] | None,
    *,
    create_bodies: list[dict[str, Any]] | None = None,
    create_response: dict[str, Any] | None = None,
) -> httpx.AsyncClient:
    """
    Build a mock server client serving a parent snapshot and child creates.

    :param parent: JSON for ``GET /v1/sessions/conv_parent``; ``None`` = 404.
    :param create_bodies: Collects ``POST /v1/sessions`` bodies when given.
    :param create_response: JSON returned by ``POST /v1/sessions``.
    :returns: An ``httpx.AsyncClient`` backed by a mock transport.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        """Serve the parent snapshot, child lookup, create, and events."""
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_parent":
            if parent is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=parent)
        if request.method == "GET" and path == "/v1/sessions/conv_parent/child_sessions":
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and path == "/v1/sessions":
            if create_bodies is not None:
                create_bodies.append(json.loads(request.content))
            return httpx.Response(201, json=create_response or {"id": "conv_child"})
        if request.method == "POST" and path.endswith("/events"):
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(404, json={"error": str(request.url)})

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://server")


# ── argument parsing and mode resolution ──────────────


def test_dispatch_args_parse_worktree_fields() -> None:
    """The object form of ``args`` carries ``worktree`` / ``base_branch``."""
    parsed = worktree_args_from_dispatch(
        {"args": {"input": "x", "worktree": True, "base_branch": " main "}}
    )
    assert parsed == WorktreeArgs(requested=True, base_branch="main")
    assert worktree_args_from_dispatch({"args": "plain string"}) == WorktreeArgs()


@pytest.mark.parametrize(
    "raw",
    [
        {"worktree": "yes"},
        {"base_branch": ""},
        {"base_branch": 3},
        {"worktree": False, "base_branch": "main"},
    ],
)
def test_invalid_worktree_fields_fail_loud(raw: dict[str, Any]) -> None:
    """
    Wrong types and contradictory combinations raise instead of dropping.

    :param raw: Invalid top-level ``sys_session_create`` fields.
    """
    with pytest.raises(ValueError):
        worktree_args_from_create(raw)


@pytest.mark.parametrize(
    ("request_args", "spec_default", "expected"),
    [
        (WorktreeArgs(), False, "off"),
        (WorktreeArgs(), True, "auto"),
        (WorktreeArgs(requested=True), False, "required"),
        (WorktreeArgs(requested=False), True, "off"),
        (WorktreeArgs(base_branch="main"), False, "required"),
    ],
)
def test_resolve_worktree_mode(
    request_args: WorktreeArgs, spec_default: bool, expected: str
) -> None:
    """
    An explicit dispatch choice wins over the sub-agent spec default.

    :param request_args: The dispatch's worktree fields.
    :param spec_default: The sub-agent spec's ``worktree`` flag.
    :param expected: The resolved mode.
    """
    assert resolve_worktree_mode(request_args, spec_default=spec_default) == expected


@pytest.mark.parametrize(
    "session_name",
    ["claude_code-1", "Fix Login Bug!", "../../etc", "a" * 200, "..."],
)
def test_branch_names_are_valid_refs(session_name: str) -> None:
    """
    Arbitrary LLM-chosen titles always map to a valid, prefixed ref.

    :param session_name: Child instance label.
    """
    branch = subagent_branch_name(session_name, "subagent_a1b2c3d4e5f6")
    validate_branch_name(branch)
    assert branch.startswith("omni/")
    assert branch.endswith("-d4e5f6")


def test_handle_fields_only_when_worktree_created() -> None:
    """The tool result reports the worktree only when the server made one."""
    assert worktree_handle_fields(
        {"workspace": "/w/repo-worktrees/x", "git_branch": "omni/x"}
    ) == {"worktree": {"path": "/w/repo-worktrees/x", "branch": "omni/x"}}
    assert worktree_handle_fields({"workspace": "/w/repo", "git_branch": None}) == {}


# ── create-field building ─────────────────────────────


@pytest.mark.asyncio
async def test_create_fields_fork_from_parent_worktree_branch(git_repo: Path) -> None:
    """
    A parent already in a worktree seeds the child's base branch.

    :param git_repo: Initialized repository fixture.
    """
    parent = {"host_id": "host_1", "workspace": str(git_repo), "git_branch": "feature/x"}
    async with _server(parent) as client:
        fields = await build_worktree_create_fields(
            mode="auto",
            request=WorktreeArgs(),
            server_client=client,
            parent_session_id="conv_parent",
            branch_name="omni/worker-1-abc123",
        )
    assert fields == {
        "host_id": "host_1",
        "workspace": str(git_repo),
        "git": {"branch_name": "omni/worker-1-abc123", "base_branch": "feature/x"},
    }


@pytest.mark.asyncio
async def test_explicit_base_branch_wins(git_repo: Path) -> None:
    """
    ``base_branch`` from the dispatch overrides the parent's branch.

    :param git_repo: Initialized repository fixture.
    """
    parent = {"host_id": "host_1", "workspace": str(git_repo), "git_branch": "feature/x"}
    async with _server(parent) as client:
        fields = await build_worktree_create_fields(
            mode="required",
            request=WorktreeArgs(requested=True, base_branch="main"),
            server_client=client,
            parent_session_id="conv_parent",
            branch_name="omni/worker-1-abc123",
        )
    assert isinstance(fields, dict)
    assert fields["git"] == {"branch_name": "omni/worker-1-abc123", "base_branch": "main"}


@pytest.mark.asyncio
async def test_auto_mode_skips_non_git_workspace(tmp_path: Path) -> None:
    """
    ``auto`` never turns a plain directory into a failed dispatch.

    :param tmp_path: Pytest temporary directory (not a git repo).
    """
    parent = {"host_id": "host_1", "workspace": str(tmp_path)}
    async with _server(parent) as client:
        fields = await build_worktree_create_fields(
            mode="auto",
            request=WorktreeArgs(),
            server_client=client,
            parent_session_id="conv_parent",
            branch_name="omni/worker-1-abc123",
        )
    assert fields is None


@pytest.mark.asyncio
async def test_required_mode_fails_without_host_binding() -> None:
    """An explicit request errors when the parent has no host/workspace."""
    async with _server({"host_id": None, "workspace": None}) as client:
        fields = await build_worktree_create_fields(
            mode="required",
            request=WorktreeArgs(requested=True),
            server_client=client,
            parent_session_id="conv_parent",
            branch_name="omni/worker-1-abc123",
        )
    assert isinstance(fields, str)
    assert fields.startswith("Error:")
    assert "host" in fields


@pytest.mark.asyncio
async def test_auto_mode_tolerates_missing_parent_snapshot() -> None:
    """``auto`` degrades to no isolation when the parent can't be read."""
    async with _server(None) as client:
        fields = await build_worktree_create_fields(
            mode="auto",
            request=WorktreeArgs(),
            server_client=client,
            parent_session_id="conv_parent",
            branch_name="omni/worker-1-abc123",
        )
    assert fields is None


# ── end-to-end through the dispatch handlers ──────────


def _parent_spec(*, worktree_default: bool) -> SimpleNamespace:
    """
    Build a parent-spec stub declaring one ``worker`` sub-agent.

    :param worktree_default: The worker spec's ``worktree`` flag.
    :returns: A structural parent-spec stub for ``execute_tool``.
    """
    executor = SimpleNamespace(type="omnigent", config={"harness": "claude-sdk"})
    worker = SimpleNamespace(name="worker", executor=executor, worktree=worktree_default)
    return SimpleNamespace(sub_agents=[worker])


async def _send(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_spec: Any,
    parent: dict[str, Any],
    args: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Drive one fresh-create ``sys_session_send`` against a mock server.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param agent_spec: The parent spec under test.
    :param parent: Parent session snapshot served by the mock server.
    :param args: The dispatch's ``args`` object.
    :returns: The parsed tool result and the captured create bodies.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    bodies: list[dict[str, Any]] = []
    created = {"id": "conv_child"}
    if (
        any(key in args for key in ("worktree", "base_branch"))
        or agent_spec.sub_agents[0].worktree
    ):
        created |= {"workspace": "/r/repo-worktrees/omni-worker", "git_branch": "omni/worker"}
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    async with _server(parent, create_bodies=bodies, create_response=created) as client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps({"agent": "worker", "title": "login", "args": args}),
                server_client=client,
                conversation_id="conv_parent",
                agent_spec=agent_spec,
                session_inbox=inbox,
            )
        finally:
            runner_app.unregister_subagent_work("conv_child")
            runner_app._session_inboxes_ref.pop("conv_parent", None)
    try:
        return json.loads(output), bodies
    except json.JSONDecodeError:
        return {"error": output}, bodies


@pytest.mark.asyncio
async def test_send_with_worktree_requests_server_worktree(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    """
    ``worktree: true`` adds host, workspace and a git block to the create.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param git_repo: Initialized repository fixture.
    """
    parent = {"id": "conv_parent", "host_id": "host_1", "workspace": str(git_repo)}
    result, bodies = await _send(
        monkeypatch,
        agent_spec=_parent_spec(worktree_default=False),
        parent=parent,
        args={"input": "fix login", "worktree": True},
    )
    assert result["status"] == "launching", result
    assert result["worktree"] == {"path": "/r/repo-worktrees/omni-worker", "branch": "omni/worker"}
    body = bodies[0]
    assert body["host_id"] == "host_1"
    assert body["workspace"] == str(git_repo)
    assert body["git"]["branch_name"].startswith("omni/login-")
    assert "base_branch" not in body["git"]


@pytest.mark.asyncio
async def test_spec_default_isolates_without_explicit_arg(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    """
    A worker spec with ``worktree: true`` isolates every new dispatch.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param git_repo: Initialized repository fixture.
    """
    parent = {"id": "conv_parent", "host_id": "host_1", "workspace": str(git_repo)}
    _, bodies = await _send(
        monkeypatch,
        agent_spec=_parent_spec(worktree_default=True),
        parent=parent,
        args={"input": "fix login"},
    )
    assert "git" in bodies[0]


@pytest.mark.asyncio
async def test_default_dispatch_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    """
    Without opt-in the create body carries no host/workspace/git fields.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param git_repo: Initialized repository fixture.
    """
    parent = {"id": "conv_parent", "host_id": "host_1", "workspace": str(git_repo)}
    result, bodies = await _send(
        monkeypatch,
        agent_spec=_parent_spec(worktree_default=False),
        parent=parent,
        args={"input": "fix login"},
    )
    assert "worktree" not in result
    assert not {"host_id", "workspace", "git"} & bodies[0].keys()


@pytest.mark.asyncio
async def test_explicit_false_overrides_spec_default(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    """
    ``worktree: false`` opts a single dispatch out of the spec default.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param git_repo: Initialized repository fixture.
    """
    parent = {"id": "conv_parent", "host_id": "host_1", "workspace": str(git_repo)}
    _, bodies = await _send(
        monkeypatch,
        agent_spec=_parent_spec(worktree_default=True),
        parent=parent,
        args={"input": "fix login", "worktree": False},
    )
    assert "git" not in bodies[0]


@pytest.mark.asyncio
async def test_required_worktree_without_host_does_not_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An impossible explicit request fails before any child is created.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    result, bodies = await _send(
        monkeypatch,
        agent_spec=_parent_spec(worktree_default=False),
        parent={"id": "conv_parent", "host_id": None, "workspace": None},
        args={"input": "fix login", "worktree": True},
    )
    assert "error" in result
    assert bodies == []


@pytest.mark.asyncio
async def test_session_create_with_worktree(git_repo: Path) -> None:
    """
    ``sys_session_create`` forwards the worktree request for ``agent_id``.

    :param git_repo: Initialized repository fixture.
    """
    from omnigent.runner.tool_dispatch import _execute_session_create

    parent = {"id": "conv_parent", "host_id": "host_1", "workspace": str(git_repo)}
    bodies: list[dict[str, Any]] = []
    created = {
        "id": "conv_child",
        "agent_name": "helper",
        "workspace": "/r/repo-worktrees/omni-review",
        "git_branch": "omni/review",
    }
    async with _server(parent, create_bodies=bodies, create_response=created) as client:
        output = await _execute_session_create(
            {"agent_id": "ag_helper", "title": "review", "worktree": True, "base_branch": "main"},
            server_client=client,
            conversation_id="conv_parent",
            publish_event=None,
        )
    result = json.loads(output)
    assert result["worktree"]["branch"] == "omni/review"
    assert bodies[0]["git"]["base_branch"] == "main"
    assert bodies[0]["git"]["branch_name"].startswith("omni/review-")


@pytest.mark.asyncio
async def test_session_create_rejects_worktree_with_config_path() -> None:
    """The multipart ``config_path`` create cannot carry a worktree."""
    from omnigent.runner.tool_dispatch import _execute_session_create

    async with _server({}) as client:
        output = await _execute_session_create(
            {"config_path": "helper.yaml", "worktree": True},
            server_client=client,
            conversation_id="conv_parent",
            publish_event=None,
        )
    assert "only with 'agent_id'" in json.loads(output)["error"]


async def _send_with_create_handler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_spec: Any,
    parent: dict[str, Any],
    args: dict[str, Any],
    on_create: Any,
) -> tuple[str, list[dict[str, Any]]]:
    """
    Drive one ``sys_session_send`` with a scripted ``POST /v1/sessions``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param agent_spec: The parent spec under test.
    :param parent: Parent session snapshot served by the mock server.
    :param args: The dispatch's ``args`` object.
    :param on_create: ``(attempt, body) -> httpx.Response`` for each create.
    :returns: The raw tool output and every create body sent.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    bodies: list[dict[str, Any]] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        """Serve the parent, empty child list, scripted creates, events."""
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_parent":
            return httpx.Response(200, json=parent)
        if request.method == "GET" and path == "/v1/sessions/conv_parent/child_sessions":
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and path == "/v1/sessions":
            body = json.loads(request.content)
            bodies.append(body)
            return on_create(len(bodies), body)
        if request.method == "POST" and path.endswith("/events"):
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(404, json={"error": str(request.url)})

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://server"
    ) as client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps({"agent": "worker", "args": args}),
                server_client=client,
                conversation_id="conv_parent",
                agent_spec=agent_spec,
                session_inbox=inbox,
            )
        finally:
            runner_app.unregister_subagent_work("conv_child")
            runner_app._session_inboxes_ref.pop("conv_parent", None)
    return output, bodies


@pytest.mark.asyncio
async def test_auto_mode_falls_back_when_worktree_creation_fails(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    """
    A spec-default isolation that the host cannot provide (offline host,
    git error) degrades to an unisolated child instead of failing.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param git_repo: Initialized repository fixture.
    """

    def _on_create(attempt: int, body: dict[str, Any]) -> httpx.Response:
        if "git" in body:
            return httpx.Response(409, json={"error": {"message": "host is offline"}})
        return httpx.Response(201, json={"id": "conv_child"})

    output, bodies = await _send_with_create_handler(
        monkeypatch,
        agent_spec=_parent_spec(worktree_default=True),
        parent={"id": "conv_parent", "host_id": "host_1", "workspace": str(git_repo)},
        args={"input": "fix login"},
        on_create=_on_create,
    )
    assert json.loads(output)["status"] == "launching", output
    assert len(bodies) == 2
    assert "git" in bodies[0]
    assert not {"host_id", "workspace", "git"} & bodies[1].keys()
    # The fallback kept the auto-assigned title: no ordinal was burned.
    assert bodies[0]["title"] == bodies[1]["title"]


@pytest.mark.asyncio
async def test_required_worktree_conflict_is_not_retried_as_name_clash(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    """
    A 409 from worktree creation is not a (parent, title) clash, so an
    explicit request fails once instead of bumping ordinals.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param git_repo: Initialized repository fixture.
    """
    output, bodies = await _send_with_create_handler(
        monkeypatch,
        agent_spec=_parent_spec(worktree_default=False),
        parent={"id": "conv_parent", "host_id": "host_1", "workspace": str(git_repo)},
        args={"input": "fix login", "worktree": True},
        on_create=lambda _n, _b: httpx.Response(409, text="host is offline"),
    )
    assert output.startswith("Error: failed to create child session: 409")
    assert len(bodies) == 1


@pytest.mark.asyncio
async def test_name_clash_still_bumps_the_ordinal(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path
) -> None:
    """
    A genuine duplicate-title 409 keeps the ordinal retry, with a fresh
    branch name per attempt.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param git_repo: Initialized repository fixture.
    """

    def _on_create(attempt: int, body: dict[str, Any]) -> httpx.Response:
        if attempt == 1:
            return httpx.Response(409, text="sub-agent name already exists under parent")
        return httpx.Response(201, json={"id": "conv_child"})

    output, bodies = await _send_with_create_handler(
        monkeypatch,
        agent_spec=_parent_spec(worktree_default=False),
        parent={"id": "conv_parent", "host_id": "host_1", "workspace": str(git_repo)},
        args={"input": "fix login", "worktree": True},
        on_create=_on_create,
    )
    assert json.loads(output)["status"] == "launching", output
    assert len(bodies) == 2
    assert bodies[0]["title"] != bodies[1]["title"]
    assert bodies[0]["git"]["branch_name"] != bodies[1]["git"]["branch_name"]
