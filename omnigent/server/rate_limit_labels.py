"""Persist a harness's subscription rate-limit state as conversation labels.

Claude's CLI reports transitions of its rate-limit state (``rate_limit_event``:
``allowed`` → ``allowed_warning`` → ``rejected``). The runner relays them as
``session.rate_limit`` SSE events; that edge is SSE-only and gone on reload, so
the server keeps the latest state as runner-owned labels. An orchestrator reads
them from the session snapshot (``labels``) and can keep new work away from a
subscription that nears its limit, instead of only learning of it from a
``rate_limit_exceeded`` failure. Empty strings clear a field: the label store is
upsert-only.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from omnigent.stores import ConversationStore

_logger = logging.getLogger(__name__)

RATE_LIMIT_STATUS_LABEL = "omnigent.rate_limit_status"
RATE_LIMIT_TYPE_LABEL = "omnigent.rate_limit_type"
RATE_LIMIT_UTILIZATION_LABEL = "omnigent.rate_limit_utilization"
RATE_LIMIT_RESETS_AT_LABEL = "omnigent.rate_limit_resets_at"

_STATUSES = frozenset({"allowed", "allowed_warning", "rejected"})


def rate_limit_label_updates(event: dict[str, Any]) -> dict[str, str] | None:
    """
    The label upserts for one ``session.rate_limit`` event.

    :param event: The decoded SSE event, e.g. ``{"type": "session.rate_limit",
        "status": "allowed_warning", "rate_limit_type": "five_hour",
        "utilization": 0.92, "resets_at": 1792700000}``.
    :returns: Label key → value (``""`` for an absent field), or ``None`` when the
        event carries no known status.
    """
    status = event.get("status")
    if status not in _STATUSES:
        return None
    rate_limit_type = event.get("rate_limit_type")
    utilization = event.get("utilization")
    resets_at = event.get("resets_at")
    return {
        RATE_LIMIT_STATUS_LABEL: str(status),
        RATE_LIMIT_TYPE_LABEL: rate_limit_type if isinstance(rate_limit_type, str) else "",
        RATE_LIMIT_UTILIZATION_LABEL: f"{float(utilization):.4f}"
        if isinstance(utilization, (int, float)) and not isinstance(utilization, bool)
        else "",
        RATE_LIMIT_RESETS_AT_LABEL: str(int(resets_at))
        if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool)
        else "",
    }


async def persist_rate_limit_labels(
    session_id: str,
    event: dict[str, Any],
    conversation_store: ConversationStore,
) -> None:
    """
    Store the latest rate-limit state of a session as its labels.

    Best effort: a store failure is logged and never breaks the relay.

    :param session_id: Session/conversation identifier.
    :param event: The decoded ``session.rate_limit`` SSE event.
    :param conversation_store: Store used to upsert labels.
    """
    updates = rate_limit_label_updates(event)
    if updates is None:
        return
    try:
        await asyncio.to_thread(conversation_store.set_labels, session_id, updates)
    except Exception:  # noqa: BLE001 — best effort, the live event still goes out
        _logger.exception(
            "Failed to persist rate-limit labels for %s",
            session_id,
            extra={"session_id": session_id},
        )
