"""``/comments/send`` with ``target_session_id``: server-side delivery.

Review comments made on a sub-agent's worktree can go to the orchestrator
(an ancestor) or to the sub-agent's own session. The server posts the
message itself, so it lands regardless of which chat the user has open,
and marks the comments addressed only after delivery succeeded.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import pytest_asyncio

from omnigent.db.utils import generate_agent_id
from omnigent.server.routes import comments as comments_routes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest_asyncio.fixture()
async def tree(db_uri: str) -> dict[str, str]:
    """
    Seed an orchestrator with one worktree child and an unrelated session.

    :param db_uri: Per-test SQLite URI.
    :returns: ``{"parent", "child", "stranger"}`` session ids.
    """
    agent_id = generate_agent_id()
    SqlAlchemyAgentStore(db_uri).create(agent_id, name="orch", bundle_location="test:///b")
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(agent_id=agent_id, title="orchestrator")
    child = store.create_conversation(
        agent_id=agent_id,
        parent_conversation_id=parent.id,
        title="worker:login",
        workspace="/r/repo-worktrees/omni-login-a1b2c3",
        git_branch="omni/login-a1b2c3",
        git_base_branch="main",
    )
    stranger = store.create_conversation(agent_id=agent_id, title="other")
    return {"parent": parent.id, "child": child.id, "stranger": stranger.id}


@pytest.fixture()
def deliveries(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """
    Capture in-process deliveries instead of posting session events.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: The ``(target_session_id, text)`` pairs delivered.
    """
    sent: list[tuple[str, str]] = []

    async def _capture(request: Any, session_id: str, text: str) -> None:
        sent.append((session_id, text))

    monkeypatch.setattr(comments_routes, "_deliver_message", _capture)
    return sent


async def _comment(client: httpx.AsyncClient, session_id: str) -> str:
    """
    Add one draft comment and return its id.

    :param client: Test client.
    :param session_id: Session to comment on.
    :returns: The comment id.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "src/login.py",
            "body": "Handle the empty password",
            "start_index": 4,
            "end_index": 12,
            "anchor_content": "password",
        },
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["id"])


async def _status(client: httpx.AsyncClient, session_id: str) -> str:
    """
    Read the single comment's status.

    :param client: Test client.
    :param session_id: Session owning the comment.
    :returns: Its status, e.g. ``"draft"``.
    """
    payload = (await client.get(f"/v1/sessions/{session_id}/comments")).json()
    rows = payload["data"] if isinstance(payload, dict) else payload
    return str(rows[0]["status"])


async def test_send_to_orchestrator_names_the_worktree(
    client: httpx.AsyncClient, tree: dict[str, str], deliveries: list[tuple[str, str]]
) -> None:
    """Comments on a child are delivered to its parent with worktree context."""
    cid = await _comment(client, tree["child"])
    resp = await client.post(
        f"/v1/sessions/{tree['child']}/comments/send",
        json={"comment_ids": [cid], "target_session_id": tree["parent"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["delivered_to"] == tree["parent"]
    [(target, text)] = deliveries
    assert target == tree["parent"]
    assert "worker:login" in text
    assert "omni/login-a1b2c3 → main" in text
    assert f'sys_session_send(session_id="{tree["child"]}"' in text
    assert "Handle the empty password" in text
    assert await _status(client, tree["child"]) == "addressed"


async def test_send_to_own_session_has_no_routing_header(
    client: httpx.AsyncClient, tree: dict[str, str], deliveries: list[tuple[str, str]]
) -> None:
    """Delivering to the task's own session sends the plain review message."""
    cid = await _comment(client, tree["child"])
    resp = await client.post(
        f"/v1/sessions/{tree['child']}/comments/send",
        json={"comment_ids": [cid], "target_session_id": tree["child"]},
    )
    assert resp.status_code == 200, resp.text
    [(target, text)] = deliveries
    assert target == tree["child"]
    assert text.startswith("Please address the following review comments.")


async def test_target_outside_the_ancestry_is_rejected(
    client: httpx.AsyncClient, tree: dict[str, str], deliveries: list[tuple[str, str]]
) -> None:
    """Comments cannot be pushed into an unrelated session."""
    cid = await _comment(client, tree["child"])
    resp = await client.post(
        f"/v1/sessions/{tree['child']}/comments/send",
        json={"comment_ids": [cid], "target_session_id": tree["stranger"]},
    )
    assert resp.status_code == 400
    assert deliveries == []
    assert await _status(client, tree["child"]) == "draft"


async def test_failed_delivery_keeps_comments_draft(
    client: httpx.AsyncClient, tree: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected delivery leaves the comments unaddressed for a retry."""
    from omnigent.errors import ErrorCode, OmnigentError

    async def _reject(request: Any, session_id: str, text: str) -> None:
        raise OmnigentError("runner offline", code=ErrorCode.CONFLICT)

    monkeypatch.setattr(comments_routes, "_deliver_message", _reject)
    cid = await _comment(client, tree["child"])
    resp = await client.post(
        f"/v1/sessions/{tree['child']}/comments/send",
        json={"comment_ids": [cid], "target_session_id": tree["parent"]},
    )
    assert resp.status_code == 409
    assert await _status(client, tree["child"]) == "draft"
