"""End-to-end: an orchestrator's sub-agent runs in its own git worktree.

A real server, a real host daemon and a real runner; the LLM is the mock
server. The parent session is host-bound to a git repository. Its worker
sub-agent declares ``worktree: true``, so the ``sys_session_send`` dispatch
must create a sibling worktree on a fresh ``omni/...`` branch and bind it as
the child's workspace, while the parent's checkout stays untouched.

No real credentials needed::

    uv run --no-sync pytest tests/e2e/test_subagent_worktree_e2e.py -v
"""

from __future__ import annotations

import json
import signal
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import POLL_INTERVAL_S, configure_mock_llm, lookup_agent_id, upload_agent
from tests.e2e.test_host_e2e import _spawn_host_daemon, _wait_for_host_online

pytestmark = [pytest.mark.timeout(600, method="signal")]

_PARENT_MODEL = "gpt-5.4-worktree-parent"
_WORKER_MODEL = "gpt-5.4-worktree-worker"
_MARKER = "WORKTREE_WORKER_DONE"


def _init_repo(path: Path) -> None:
    """
    Create a git repository with one commit on ``main``.

    :param path: Directory to initialize, e.g. ``tmp_path / "repo"``.
    """
    path.mkdir()
    git = ["git", "-C", str(path), "-c", "user.name=e2e", "-c", "user.email=e2e@example.com"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    # The host daemon runs with a temp HOME; landing a task commits there.
    subprocess.run([*git, "config", "user.name", "e2e"], check=True)
    subprocess.run([*git, "config", "user.email", "e2e@example.com"], check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run([*git, "add", "README.md"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)


def _write_orchestrator_yaml(tmp_path: Path) -> Path:
    """
    Write an orchestrator agent whose worker sub-agent opts into worktrees.

    :param tmp_path: Pytest temp directory.
    :returns: Path to the agent directory.
    """
    agent_dir = tmp_path / "worktree-orchestrator"
    agent_dir.mkdir()
    (agent_dir / "worktree-orchestrator.yaml").write_text(
        "\n".join(
            [
                "name: worktree-orchestrator",
                "description: Dispatches one worker for the worktree e2e test.",
                "executor:",
                "  harness: openai-agents",
                f"  model: {_PARENT_MODEL}",
                "os_env:",
                "  type: caller_process",
                "  sandbox:",
                "    type: none",
                "prompt: Dispatch the worker sub-agent.",
                "tools:",
                "  worker:",
                "    type: agent",
                "    description: Test worker.",
                "    worktree: true",
                "    os_env: inherit",
                "    executor:",
                "      harness: openai-agents",
                f"      model: {_WORKER_MODEL}",
                f"    prompt: Reply with {_MARKER}.",
                "",
            ]
        )
    )
    return agent_dir


def _wait_for_child(client: httpx.Client, session_id: str, timeout_s: float = 120.0) -> dict:
    """
    Poll until the parent has a child session, then return its snapshot.

    :param client: HTTP client pointed at the live server.
    :param session_id: The parent session id.
    :param timeout_s: Max seconds to wait.
    :returns: The child's ``GET /v1/sessions/{id}`` JSON.
    :raises AssertionError: If no child appears in time.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}/child_sessions")
        if resp.status_code == 200:
            children = resp.json().get("data") or []
            if children:
                child = client.get(f"/v1/sessions/{children[0]['id']}")
                child.raise_for_status()
                return child.json()
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"no child session appeared under {session_id} in {timeout_s:.0f}s")


def _wait_for_text(
    client: httpx.Client, session_id: str, marker: str, timeout_s: float = 180.0
) -> None:
    """
    Poll a session snapshot until ``marker`` appears in its items.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session to poll.
    :param marker: Substring to wait for.
    :param timeout_s: Max seconds to wait.
    :raises AssertionError: If the marker never appears.
    """
    deadline = time.monotonic() + timeout_s
    blob = ""
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        blob = json.dumps(resp.json().get("items", []))
        if marker in blob:
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"{marker!r} never appeared in {session_id}: {blob[:600]!r}")


def test_worker_subagent_runs_in_its_own_worktree(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """
    A ``worktree: true`` worker dispatched by the orchestrator gets a fresh
    worktree + branch off the parent's repo, and still completes its turn.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_wt_1",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {"agent": "worker", "title": "login", "args": "fix the login"}
                        ),
                    }
                ]
            },
            {"text": "Dispatched the worker."},
            {"text": f"Worker reported {_MARKER}."},
            {"text": "Routing the review comments to the worker."},
        ],
        key=_PARENT_MODEL,
    )
    configure_mock_llm(mock_llm_server_url, [{"text": _MARKER}], key=_WORKER_MODEL)

    daemon = _spawn_host_daemon(
        tmp_path=tmp_path, live_server=live_server, mock_llm_server_url=mock_llm_server_url
    )
    try:
        _wait_for_host_online(http_client, daemon.host_id, timeout=30.0)
        agent_id = lookup_agent_id(
            http_client, upload_agent(http_client, _write_orchestrator_yaml(tmp_path))
        )
        created = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": daemon.host_id, "workspace": str(repo)},
            timeout=60.0,
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]

        resp = http_client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "go"}]},
            },
            timeout=60.0,
        )
        assert resp.status_code in (200, 202), resp.text

        child = _wait_for_child(http_client, session_id)
        branch = child["git_branch"]
        worktree = Path(child["workspace"])
        assert isinstance(branch, str) and branch.startswith("omni/login-"), child
        # The parent's checked-out branch is recorded as the diff base.
        assert child["git_base_branch"] == "main", child
        assert worktree != repo.resolve()
        assert worktree.is_dir(), f"child worktree {worktree} missing on disk"
        head = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == branch
        listed = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert f"branch refs/heads/{branch}" in listed
        # The orchestrator's own checkout is unchanged.
        parent = http_client.get(f"/v1/sessions/{session_id}").json()
        assert parent["workspace"] == str(repo.resolve())
        assert parent["git_branch"] is None

        assert child["host_id"] is None, "a child must not own the parent's host"
        _wait_for_text(http_client, child["id"], _MARKER)

        # Review view: committed + uncommitted + untracked changes of the
        # task branch since it forked from the parent's branch.
        git = [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=e2e",
            "-c",
            "user.email=e2e@example.com",
        ]
        (worktree / "README.md").write_text("hello\nfrom the worker\n")
        subprocess.run([*git, "commit", "-q", "-am", "worker commit"], check=True)
        (worktree / "README.md").write_text("hello\nfrom the worker\nuncommitted\n")
        (worktree / "new.txt").write_text("fresh\n")
        changes = http_client.get(f"/v1/sessions/{child['id']}/resources/git/changes")
        assert changes.status_code == 200, changes.text
        body = changes.json()
        assert body["base"] == "main"
        by_path = {entry["path"]: entry for entry in body["data"]}
        assert set(by_path) == {"README.md", "new.txt"}
        assert (by_path["README.md"]["lines_added"], by_path["README.md"]["lines_removed"]) == (
            2,
            0,
        )
        assert by_path["new.txt"]["status"] == "created"
        diff = http_client.get(f"/v1/sessions/{child['id']}/resources/git/diff/README.md")
        assert diff.status_code == 200, diff.text
        assert diff.json()["before"] == "hello\n"
        assert diff.json()["after"] == "hello\nfrom the worker\nuncommitted\n"

        # The worktree has its own dev server port range.
        assert body["ports"]["index"] >= 1
        assert body["ports"]["base"] == 3000 + 10 * body["ports"]["index"]

        # Conflict prediction: a parallel branch rewriting the same line
        # really conflicts; the base does not.
        rival = tmp_path / "rival"
        subprocess.run(
            [*git, "worktree", "add", "-q", "-b", "omni/rival", str(rival), "main"], check=True
        )
        (rival / "README.md").write_text("hello\nfrom the rival\n")
        subprocess.run(
            [*git[:2], str(rival), *git[3:], "commit", "-q", "-am", "rival"], check=True
        )
        conflicts = http_client.get(
            f"/v1/sessions/{child['id']}/resources/git/conflicts",
            params={"against": "omni/rival"},
        )
        assert conflicts.status_code == 200, conflicts.text
        verdicts = {item["ref"]: item for item in conflicts.json()["results"]}
        assert verdicts["main"]["clean"] is True
        assert verdicts["omni/rival"] == {
            "ref": "omni/rival",
            "clean": False,
            "files": ["README.md"],
        }
        assert conflicts.json()["dirty"] is True

        # The child's file panel reads its own worktree, not the parent repo.
        content = http_client.get(
            f"/v1/sessions/{child['id']}/resources/environments/default/filesystem/new.txt"
        )
        assert content.status_code == 200, content.text
        assert "fresh" in json.dumps(content.json())

        # Review comments on the worker's worktree go to the orchestrator.
        comment = http_client.post(
            f"/v1/sessions/{child['id']}/comments",
            json={
                "path": "README.md",
                "body": "Say goodbye too",
                "start_index": 0,
                "end_index": 5,
                "anchor_content": "hello",
            },
        )
        assert comment.status_code == 200, comment.text
        sent = http_client.post(
            f"/v1/sessions/{child['id']}/comments/send",
            json={"comment_ids": [comment.json()["id"]], "target_session_id": session_id},
            timeout=60.0,
        )
        assert sent.status_code == 200, sent.text
        _wait_for_text(http_client, session_id, "Review comments on sub-agent")
        _wait_for_text(http_client, session_id, "Say goodbye too")

        # Landing the task: commit the rest, then merge the branch into main
        # in the orchestrator's checkout.
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "worker wrap-up"], check=True)
        merged = http_client.post(
            f"/v1/sessions/{child['id']}/resources/git/merge",
            json={"strategy": "squash", "message": "Land login task"},
            timeout=120.0,
        )
        assert merged.status_code == 200, merged.text
        assert merged.json()["base"] == "main"
        assert (repo / "new.txt").read_text() == "fresh\n"
        head_subject = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head_subject == "Land login task"

        # Stopping the worker must not tear down the orchestrator's runner:
        # the child shares it, so the parent stays online afterwards.
        runner_id = parent["runner_id"]
        stop = http_client.post(
            f"/v1/sessions/{child['id']}/events", json={"type": "stop_session", "data": {}}
        )
        assert stop.status_code < 400, stop.text
        time.sleep(3)
        status = http_client.get(f"/v1/runners/{runner_id}/status")
        assert status.status_code == 200 and status.json().get("online") is True, status.text
    finally:
        daemon.proc.send_signal(signal.SIGTERM)
        try:
            daemon.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.proc.kill()
            daemon.proc.wait()


def test_failed_background_setup_fails_the_worker_and_tells_the_orchestrator(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """
    A ``setup_async`` that fails holds the worker's first turn, then fails it,
    and the orchestrator is woken with the setup error instead of a result.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".omnigent").mkdir()
    (repo / ".omnigent" / "worktree.yaml").write_text(
        "setup_async: sleep 2; echo cold install broke; exit 5\n"
    )
    git = ["git", "-C", str(repo), "-c", "user.name=e2e", "-c", "user.email=e2e@example.com"]
    subprocess.run([*git, "add", ".omnigent"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "worktree setup"], check=True)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_wt_setup",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {"agent": "worker", "title": "deps", "args": "run the tests"}
                        ),
                    }
                ]
            },
            {"text": "Dispatched the worker."},
            {
                "tool_calls": [
                    {"call_id": "call_inbox", "name": "sys_read_inbox", "arguments": "{}"}
                ]
            },
            {"text": "The worker's worktree setup failed."},
        ],
        key=_PARENT_MODEL,
    )
    configure_mock_llm(mock_llm_server_url, [{"text": _MARKER}], key=_WORKER_MODEL)

    daemon = _spawn_host_daemon(
        tmp_path=tmp_path, live_server=live_server, mock_llm_server_url=mock_llm_server_url
    )
    try:
        _wait_for_host_online(http_client, daemon.host_id, timeout=30.0)
        agent_id = lookup_agent_id(
            http_client, upload_agent(http_client, _write_orchestrator_yaml(tmp_path))
        )
        created = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": daemon.host_id, "workspace": str(repo)},
            timeout=60.0,
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        resp = http_client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "go"}]},
            },
            timeout=60.0,
        )
        assert resp.status_code in (200, 202), resp.text

        child = _wait_for_child(http_client, session_id)
        # The inbox result the orchestrator reads carries the setup's output.
        _wait_for_text(http_client, session_id, "cold install broke")
        child_items = json.dumps(
            http_client.get(f"/v1/sessions/{child['id']}").json().get("items", [])
        )
        assert _MARKER not in child_items, "the worker ran although its setup failed"
        assert Path(child["workspace"]).is_dir(), "the failed worktree is kept for inspection"
    finally:
        daemon.proc.send_signal(signal.SIGTERM)
        try:
            daemon.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.proc.kill()
            daemon.proc.wait()


def test_broken_worktree_config_fails_the_dispatch_instead_of_running_unisolated(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """
    A ``worktree.yaml`` the host cannot read fails the ``worktree: true``
    dispatch with a clear error; no worker runs in the orchestrator's checkout.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".omnigent").mkdir()
    # Unquoted ": " inside the command makes this invalid YAML.
    (repo / ".omnigent" / "worktree.yaml").write_text("setup_async: sleep 1; echo ERR: x\n")
    git = ["git", "-C", str(repo), "-c", "user.name=e2e", "-c", "user.email=e2e@example.com"]
    subprocess.run([*git, "add", ".omnigent"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "broken worktree config"], check=True)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_wt_bad_config",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {"agent": "worker", "title": "login", "args": "fix the login"}
                        ),
                    }
                ]
            },
            {"text": "The worktree config is broken."},
        ],
        key=_PARENT_MODEL,
    )
    configure_mock_llm(mock_llm_server_url, [{"text": _MARKER}], key=_WORKER_MODEL)

    daemon = _spawn_host_daemon(
        tmp_path=tmp_path, live_server=live_server, mock_llm_server_url=mock_llm_server_url
    )
    try:
        _wait_for_host_online(http_client, daemon.host_id, timeout=30.0)
        agent_id = lookup_agent_id(
            http_client, upload_agent(http_client, _write_orchestrator_yaml(tmp_path))
        )
        created = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": daemon.host_id, "workspace": str(repo)},
            timeout=60.0,
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        resp = http_client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "go"}]},
            },
            timeout=60.0,
        )
        assert resp.status_code in (200, 202), resp.text

        _wait_for_text(http_client, session_id, "could not prepare a worktree")
        _wait_for_text(http_client, session_id, "The worktree config is broken.")
        children = http_client.get(f"/v1/sessions/{session_id}/child_sessions").json()
        assert not children.get("data"), f"a worker was dispatched unisolated: {children}"
    finally:
        daemon.proc.send_signal(signal.SIGTERM)
        try:
            daemon.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.proc.kill()
            daemon.proc.wait()
