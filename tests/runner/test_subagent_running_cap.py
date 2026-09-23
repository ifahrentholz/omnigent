"""``max_running_subagents`` bounds how many sub-agent dispatches run at once.

``spawn_bounds`` only caps dispatches per turn; with "fire a task, then the
next" usage the orchestrator's fleet grew without bound. The cap counts the
runner's unfinished dispatches for the parent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml

_PARENT = "conv_cap_parent"


def _spec(cap: int | None) -> SimpleNamespace:
    """
    Build a parent-spec stub with one ``worker`` sub-agent.

    :param cap: The parent's ``max_running_subagents``.
    :returns: A structural parent-spec stub.
    """
    executor = SimpleNamespace(type="omnigent", config={"harness": "claude-sdk"})
    return SimpleNamespace(
        sub_agents=[SimpleNamespace(name="worker", executor=executor)],
        max_running_subagents=cap,
    )


async def _dispatch(spec: Any, title: str) -> tuple[str, int]:
    """
    Send one named dispatch against a mock server.

    :param spec: The parent spec stub.
    :param title: Child title for the dispatch.
    :returns: The tool output and the number of child creates.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    creates: list[dict[str, Any]] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        """Serve an empty child list, creates and message posts."""
        path = request.url.path
        if request.method == "GET" and path == f"/v1/sessions/{_PARENT}/child_sessions":
            return httpx.Response(200, json={"data": []})
        if request.method == "GET" and path == f"/v1/sessions/{_PARENT}":
            return httpx.Response(200, json={"id": _PARENT})
        if request.method == "POST" and path == "/v1/sessions":
            creates.append(json.loads(request.content))
            return httpx.Response(201, json={"id": f"conv_cap_{title}"})
        if request.method == "POST" and path.endswith("/events"):
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(404, json={"error": str(request.url)})

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://server"
    ) as client:
        output = await execute_tool(
            tool_name="sys_session_send",
            arguments=json.dumps({"agent": "worker", "title": title, "args": "work"}),
            server_client=client,
            conversation_id=_PARENT,
            agent_spec=spec,
            session_inbox=inbox,
        )
    runner_app._session_inboxes_ref.pop(_PARENT, None)
    return output, len(creates)


@pytest.fixture
def running_children(monkeypatch: pytest.MonkeyPatch) -> Any:
    """
    Register fake unfinished dispatches for the parent and clean up after.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: A ``register(n)`` helper returning the child ids.
    """
    from omnigent.runner import app as runner_app

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    registered: list[str] = []

    def _register(count: int) -> list[str]:
        for index in range(count):
            child = f"conv_cap_running_{index}"
            runner_app.register_subagent_work(
                parent_session_id=_PARENT,
                child_session_id=child,
                agent="worker",
                title=f"r{index}",
            )
            registered.append(child)
        return registered

    yield _register
    for child in [*registered, "conv_cap_new", "conv_cap_extra"]:
        runner_app.unregister_subagent_work(child)


@pytest.mark.asyncio
async def test_dispatch_beyond_cap_is_refused(running_children: Any) -> None:
    """
    With two dispatches running and a cap of two, a third new dispatch is
    refused before any child is created.

    :param running_children: Fixture registering unfinished dispatches.
    """
    running_children(2)
    output, creates = await _dispatch(_spec(2), "extra")
    assert output.startswith("Error: 2 sub-agents are already running"), output
    assert creates == 0


@pytest.mark.asyncio
async def test_finished_dispatches_free_their_slot(running_children: Any) -> None:
    """
    A completed dispatch no longer counts against the cap.

    :param running_children: Fixture registering unfinished dispatches.
    """
    from omnigent.runner import app as runner_app

    children = running_children(2)
    runner_app.mark_subagent_work_terminal(children[0], status="completed", output="done")
    output, creates = await _dispatch(_spec(2), "new")
    assert json.loads(output)["status"] == "launching", output
    assert creates == 1


@pytest.mark.asyncio
async def test_no_cap_means_unlimited(running_children: Any) -> None:
    """
    Without ``max_running_subagents`` dispatch behaviour is unchanged.

    :param running_children: Fixture registering unfinished dispatches.
    """
    running_children(5)
    output, creates = await _dispatch(_spec(None), "new")
    assert json.loads(output)["status"] == "launching", output
    assert creates == 1


def test_spec_parses_cap_and_rejects_invalid(tmp_path: Path) -> None:
    """
    ``max_running_subagents`` round-trips from YAML and must be >= 1.

    :param tmp_path: Pytest temporary directory.
    """
    from omnigent.spec.parser import parse

    config: dict[str, object] = {"spec_version": 1, "name": "boss", "max_running_subagents": 3}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    assert parse(tmp_path).max_running_subagents == 3
    config["max_running_subagents"] = 0
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(ValueError, match="max_running_subagents"):
        parse(tmp_path)


def test_legacy_yaml_cap_reaches_the_runner_spec() -> None:
    """The legacy loader path carries the cap into the translated spec."""
    from omnigent.inner.loader import load_agent_def
    from omnigent.spec.omnigent import agent_def_to_agent_spec

    agent_def = load_agent_def({"name": "boss", "max_running_subagents": 4})
    assert agent_def_to_agent_spec(agent_def).max_running_subagents == 4


@pytest.mark.asyncio
async def test_parallel_dispatches_cannot_overshoot_the_cap(running_children: Any) -> None:
    """
    Four sends in one model response with a cap of two start exactly two:
    slots are claimed before the first network call, not on registration.

    :param running_children: Fixture registering unfinished dispatches.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    running_children(0)
    creates: list[str] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        """Slow creates so all four sends overlap."""
        path = request.url.path
        if request.method == "GET" and path == f"/v1/sessions/{_PARENT}/child_sessions":
            return httpx.Response(200, json={"data": []})
        if request.method == "GET" and path == f"/v1/sessions/{_PARENT}":
            return httpx.Response(200, json={"id": _PARENT})
        if request.method == "POST" and path == "/v1/sessions":
            body = json.loads(request.content)
            await asyncio.sleep(0.05)
            creates.append(body["title"])
            return httpx.Response(201, json={"id": f"conv_par_{len(creates)}"})
        if request.method == "POST" and path.endswith("/events"):
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(404, json={"error": str(request.url)})

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://server"
    ) as client:
        try:
            outputs = await asyncio.gather(
                *(
                    execute_tool(
                        tool_name="sys_session_send",
                        arguments=json.dumps({"agent": "worker", "title": f"t{i}", "args": "go"}),
                        server_client=client,
                        conversation_id=_PARENT,
                        agent_spec=_spec(2),
                        session_inbox=inbox,
                    )
                    for i in range(4)
                )
            )
        finally:
            for index in range(1, 5):
                runner_app.unregister_subagent_work(f"conv_par_{index}")
            runner_app._session_inboxes_ref.pop(_PARENT, None)

    assert len(creates) == 2, outputs
    assert (
        sum(output.startswith("Error: 2 sub-agents are already running") for output in outputs)
        == 2
    )
    from omnigent.runner import subagent_cap

    assert _PARENT not in subagent_cap._claims, "claims must be released after return"


@pytest.mark.asyncio
async def test_resend_by_session_id_respects_the_cap(running_children: Any) -> None:
    """
    Waking an idle child by ``session_id`` starts a new run, so it is refused
    when the cap is reached.

    :param running_children: Fixture registering unfinished dispatches.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    running_children(1)
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500)), base_url="http://server"
    ) as client:
        output = await execute_tool(
            tool_name="sys_session_send",
            arguments=json.dumps({"session_id": "conv_cap_idle", "args": "again"}),
            server_client=client,
            conversation_id=_PARENT,
            agent_spec=_spec(1),
            session_inbox=inbox,
        )
    runner_app._session_inboxes_ref.pop(_PARENT, None)
    assert output.startswith("Error: 1 sub-agents are already running"), output


@pytest.mark.asyncio
async def test_recovered_waiting_work_does_not_hold_slots(running_children: Any) -> None:
    """
    After a runner restart, interrupted children are re-registered as
    ``waiting`` with unknown state; they must not block new dispatches.

    :param running_children: Fixture registering unfinished dispatches.
    """
    from omnigent.runner import app as runner_app

    for child in running_children(2):
        runner_app.get_subagent_work(child).status = "waiting"
    output, creates = await _dispatch(_spec(2), "new")
    assert json.loads(output)["status"] == "launching", output
    assert creates == 1
