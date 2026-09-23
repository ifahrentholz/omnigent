"""E2E: the Changes tab can compare against the branch's base, not just HEAD.

A worker that commits in its worktree makes its changes vanish from the
classic "Uncommitted" list (working tree vs HEAD). The "vs <base>" baseline
lists everything the branch changed since it forked, and the choice rides in
the URL (``?diffsrc=branch``) so the file viewer diffs against the same base.

Both endpoints are intercepted with deterministic payloads so the test
exercises the toggle, the list swap and the URL contract without a real
worktree.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import open_right_rail


def _entry(path: str, status: str, added: int, removed: int) -> dict[str, object]:
    """
    Build one changed-file entry in the wire shape.

    :param path: Workspace-relative path.
    :param status: ``created`` / ``modified`` / ``deleted`` / ``renamed``.
    :param added: Lines added.
    :param removed: Lines removed.
    :returns: The entry dict.
    """
    return {
        "object": "session.environment.filesystem.entry",
        "path": path,
        "name": path.rsplit("/", 1)[-1],
        "status": status,
        "bytes": 10,
        "modified_at": 1_774_118_382,
        "lines_added": added,
        "lines_removed": removed,
    }


def test_changes_tab_switches_to_branch_baseline(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Toggling "vs main" swaps the list to the branch's changes and sets ?diffsrc=branch."""
    base_url, session_id = seeded_session
    sid = re.escape(session_id)

    def _uncommitted(route: Route) -> None:
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {"object": "list", "has_more": False, "data": [_entry("wip.py", "modified", 1, 0)]}
            ),
        )

    def _branch(route: Route) -> None:
        route.fulfill(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(
                {
                    "object": "list",
                    "has_more": False,
                    "base": "main",
                    "merge_base": "abc123",
                    "data": [
                        _entry("committed_feature.py", "created", 12, 0),
                        _entry("wip.py", "modified", 1, 0),
                    ],
                }
            ),
        )

    page.route(
        re.compile(rf"/v1/sessions/{sid}/resources/environments/[^/]+/changes(\?|$)"),
        _uncommitted,
    )
    page.route(re.compile(rf"/v1/sessions/{sid}/resources/git/changes(\?|$)"), _branch)

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    changes_tab = rail.get_by_role("tab", name=re.compile("^Changes"))
    changes_tab.click()
    expect(changes_tab).to_have_attribute("aria-selected", "true")

    # Default baseline: only the uncommitted edit.
    expect(rail.get_by_text("wip.py")).to_be_visible(timeout=30_000)
    expect(rail.get_by_text("committed_feature.py")).to_have_count(0)
    uncommitted = rail.get_by_role("radio", name="Uncommitted")
    expect(uncommitted).to_have_attribute("aria-checked", "true")

    rail.get_by_role("radio", name=re.compile("^vs ")).click()

    # Branch baseline: the committed file appears, and the base is named.
    expect(rail.get_by_text("committed_feature.py")).to_be_visible(timeout=30_000)
    expect(rail.get_by_role("radio", name="vs main")).to_have_attribute("aria-checked", "true")
    expect(page).to_have_url(re.compile(r"[?&]diffsrc=branch(&|$)"))

    # Switching back restores the HEAD baseline and drops the URL param.
    uncommitted.click()
    expect(rail.get_by_text("committed_feature.py")).to_have_count(0)
    expect(page).not_to_have_url(re.compile(r"diffsrc="))
