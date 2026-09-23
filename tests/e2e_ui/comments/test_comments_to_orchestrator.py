"""UI journey: send a sub-agent's review comments to its orchestrator.

Comments made while reviewing a worker's worktree belong to the worker's
session, but the user often wants the orchestrator to decide what happens
with them. A sub-agent's Comments panel offers "To orchestrator": the server
delivers the formatted comments into the parent session (whichever chat is
on screen) and marks them addressed.

The child session and its comment are created over the REST API so the test
stays LLM-free; delivery goes through the real ``/comments/send`` path.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail


def test_child_comments_go_to_the_orchestrator(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Clicking "To orchestrator" posts the comment into the parent session."""
    base_url, parent_id = seeded_session
    with httpx.Client(base_url=base_url, timeout=30.0) as api:
        agent_id = api.get(f"/v1/sessions/{parent_id}").json()["agent_id"]
        created = api.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "parent_session_id": parent_id, "title": "worker:login"},
        )
        assert created.status_code in (200, 201), created.text
        child_id = created.json()["id"]
        comment = api.post(
            f"/v1/sessions/{child_id}/comments",
            json={
                "path": "README.md",
                "body": "Please handle the empty password case",
                "start_index": 0,
                "end_index": 5,
                "anchor_content": "hello",
            },
        )
        assert comment.status_code == 200, comment.text

    page.goto(f"{base_url}/c/{child_id}?file=README.md")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    show_comments = rail.get_by_role("button", name="Show comments")
    expect(show_comments).to_be_visible(timeout=30_000)
    show_comments.click()
    comment_text = rail.get_by_text("Please handle the empty password case")
    expect(comment_text).to_be_visible(timeout=30_000)

    rail.get_by_role("button", name="To orchestrator").click()

    # Addressed server-side once delivered: the Open tab empties.
    expect(comment_text).to_have_count(0, timeout=30_000)
    with httpx.Client(base_url=base_url, timeout=30.0) as api:
        items = api.get(f"/v1/sessions/{parent_id}").json().get("items", [])
    delivered = [str(item) for item in items if "Review comments on sub-agent" in str(item)]
    assert delivered, "the orchestrator never received the comments"
    assert "Please handle the empty password case" in delivered[-1]
