"""Concurrent named ``sys_session_send`` calls must not race the child create.

Several sends in one model response dispatch concurrently. Two sends to the
same ``(agent, title)`` used to both miss the find-existing lookup; the second
create then hit the server's duplicate-title check and failed with a 409
instead of addressing the child the first send just created. Now the second
send finds that child and, while its first turn is launching, gets the
standard retryable "still starting" answer.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest


@pytest.mark.asyncio
async def test_concurrent_sends_to_one_title_create_one_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Two overlapping sends to ``worker:login`` create exactly one child and
    neither surfaces a name-collision error.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    children: list[dict[str, Any]] = []
    creates: list[dict[str, Any]] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        """Serve a server whose create is slow and whose list sees creates."""
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_race_parent/child_sessions":
            wanted = f"{request.url.params.get('tool')}:{request.url.params.get('session_name')}"
            return httpx.Response(
                200, json={"data": [c for c in children if c["title"] == wanted]}
            )
        if request.method == "GET" and path == "/v1/sessions/conv_race_parent":
            return httpx.Response(200, json={"id": "conv_race_parent"})
        if request.method == "POST" and path == "/v1/sessions":
            body = json.loads(request.content)
            creates.append(body)
            # Yield so an unserialized second send would reach its own
            # lookup before this child becomes visible.
            await asyncio.sleep(0.05)
            if any(c["title"] == body["title"] for c in children):
                return httpx.Response(409, text="sub-agent name already exists under parent")
            children.append({"id": "conv_race_child", "title": body["title"], "labels": {}})
            return httpx.Response(201, json={"id": "conv_race_child"})
        if request.method == "POST" and path.endswith("/events"):
            return httpx.Response(202, json={"queued": True})
        if request.method == "PATCH":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": str(request.url)})

    executor = SimpleNamespace(type="omnigent", config={"harness": "claude-sdk"})
    spec = SimpleNamespace(sub_agents=[SimpleNamespace(name="worker", executor=executor)])
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    arguments = json.dumps({"agent": "worker", "title": "login", "args": "fix the login"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://server"
    ) as client:
        try:
            outputs = await asyncio.gather(
                *(
                    execute_tool(
                        tool_name="sys_session_send",
                        arguments=arguments,
                        server_client=client,
                        conversation_id="conv_race_parent",
                        agent_spec=spec,
                        session_inbox=inbox,
                    )
                    for _ in range(2)
                )
            )
        finally:
            runner_app.unregister_subagent_work("conv_race_child")
            runner_app._session_inboxes_ref.pop("conv_race_parent", None)

    assert len(creates) == 1, "the second send must find the first send's child"
    assert json.loads(outputs[0])["conversation_id"] == "conv_race_child"
    # The child's first turn is still launching, so the second send gets the
    # retryable answer for that child rather than a duplicate-title 409.
    assert "still starting" in outputs[1], outputs[1]
    assert "409" not in outputs[1]
