"""Bound how many sub-agent dispatches of one parent run at the same time.

``max_running_subagents`` on an orchestrator spec caps its concurrently
running dispatches. A dispatch is counted from the moment it claims a slot
(before its first network call) until the runner's work registry reports it
finished, so several sends in one model response cannot all pass the check.

Claiming is synchronous: the check and the reservation happen with no
``await`` in between, which makes them atomic on the runner's event loop.
"""

from __future__ import annotations

# Recovered work sits in "waiting" until the child reports again; its real
# state is unknown after a runner restart, so it must not hold a slot.
_COUNTED_STATUSES = frozenset({"launching", "running"})

# parent session id -> tokens of dispatches that claimed a slot and have not
# returned yet. A token is the dispatch's work id when it registers work.
_claims: dict[str, set[str]] = {}


def claim_slot(
    parent_session_id: str,
    cap: int,
    token: str,
    *,
    exclude_child: str | None = None,
) -> str | None:
    """
    Reserve one running slot for a dispatch, or explain why none is free.

    :param parent_session_id: The orchestrator session id.
    :param cap: The spec's ``max_running_subagents``.
    :param token: Unique id for this dispatch, e.g. its work id.
    :param exclude_child: Child the dispatch continues; its own running work
        does not need a second slot.
    :returns: ``None`` when the slot was claimed, else an ``"Error: ..."``
        message for the orchestrator.
    """
    # Lazy: the runner app imports the dispatch module that imports this one.
    from omnigent.runner import app as _runner_app
    from omnigent.tools.builtins.web_fetch import RESEARCHER_NAME

    claimed = _claims.setdefault(parent_session_id, set())
    running = sum(
        1
        for entry in _runner_app.list_subagent_work(parent_session_id)
        if entry.status in _COUNTED_STATUSES
        and entry.child_session_id != exclude_child
        and entry.work_id not in claimed
        and entry.agent != RESEARCHER_NAME
    )
    in_use = running + len(claimed)
    if in_use >= cap:
        if not claimed:
            _claims.pop(parent_session_id, None)
        return (
            f"Error: {in_use} sub-agents are already running (max_running_subagents="
            f"{cap}). Wait for one to finish (its result arrives in your inbox) "
            "before dispatching another."
        )
    claimed.add(token)
    return None


def release_slot(parent_session_id: str, token: str) -> None:
    """
    Drop a dispatch's claim once it has returned.

    Successful dispatches stay counted through their registered work entry.

    :param parent_session_id: The orchestrator session id.
    :param token: The token passed to :func:`claim_slot`.
    """
    claimed = _claims.get(parent_session_id)
    if claimed is None:
        return
    claimed.discard(token)
    if not claimed:
        _claims.pop(parent_session_id, None)
