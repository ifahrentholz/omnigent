"""UI journey: the Agents tab's Worktrees view reviews each task's branch.

An orchestrator's sub-agents that run in their own git worktree show up in
the Worktrees view with ``branch → base`` and the branch's +/− totals. A row
expands into the branch's changed files, and each file links straight into
the child session's branch diff (``?file=…&diff=1&diffsrc=branch``).

The child list and the child's branch changes are intercepted with
deterministic payloads, so the test pins the view without spawning real
sub-agents.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import open_right_rail

_CHILD_ID = "conv_worktree_child"


def test_worktrees_view_lists_task_branches(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A worktree child shows its branch, base, totals and file deep links."""
    base_url, session_id = seeded_session

    def _children(route: Route) -> None:
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": _CHILD_ID,
                            "object": "child_session",
                            "parent_session_id": session_id,
                            "title": "worker:login",
                            "tool": "worker",
                            "session_name": "login",
                            "kind": "sub_agent",
                            "created_at": 1,
                            "updated_at": 2,
                            "current_task_status": "completed",
                            "busy": False,
                            "labels": {},
                            "pending_elicitations_count": 0,
                            "workspace": "/repo-worktrees/omni-login-a1b2c3",
                            "git_branch": "omni/login-a1b2c3",
                            "git_base_branch": "main",
                        }
                    ],
                    "has_more": False,
                }
            ),
        )

    def _branch_changes(route: Route) -> None:
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "object": "list",
                    "base": "main",
                    "merge_base": "abc123",
                    "has_more": False,
                    "data": [
                        {
                            "object": "session.environment.filesystem.entry",
                            "path": "src/login.py",
                            "name": "login.py",
                            "status": "modified",
                            "previous_path": None,
                            "bytes": 10,
                            "modified_at": 1,
                            "lines_added": 7,
                            "lines_removed": 2,
                        }
                    ],
                }
            ),
        )

    page.route(
        re.compile(rf"/v1/sessions/{re.escape(session_id)}/child_sessions(\?|$)"), _children
    )
    page.route(
        re.compile(rf"/v1/sessions/{_CHILD_ID}/resources/git/changes(\?|$)"), _branch_changes
    )

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    rail.get_by_role("button", name="Worktrees view").click()

    row = rail.locator('[data-testid="worktree-row"]')
    expect(row).to_have_count(1, timeout=30_000)
    expect(row).to_contain_text("omni/login-a1b2c3")
    expect(row).to_contain_text("main")
    expect(row).to_contain_text("+7")
    expect(row).to_contain_text("−2")

    row.get_by_role("button", name="Expand login").click()
    file_link = row.locator('[data-testid="worktree-file"]')
    expect(file_link).to_contain_text("src/login.py")
    href = urlparse(file_link.get_attribute("href") or "")
    assert href.path.endswith(f"/c/{_CHILD_ID}")
    params = parse_qs(href.query)
    assert params["file"] == ["src/login.py"]
    assert params["diff"] == ["1"]
    assert params["diffsrc"] == ["branch"]
