"""Claude's rate-limit state relayed by the runner is kept as session labels."""

from __future__ import annotations

from unittest.mock import Mock

import httpx
import pytest

from omnigent.server.rate_limit_labels import (
    RATE_LIMIT_RESETS_AT_LABEL,
    RATE_LIMIT_STATUS_LABEL,
    RATE_LIMIT_TYPE_LABEL,
    RATE_LIMIT_UTILIZATION_LABEL,
    persist_rate_limit_labels,
    rate_limit_label_updates,
)
from omnigent.server.routes._sessions import orchestration
from omnigent.stores.conversation_store import ConversationStore


def test_label_updates_clear_absent_fields_and_skip_unknown_status() -> None:
    assert rate_limit_label_updates(
        {
            "status": "allowed_warning",
            "rate_limit_type": "five_hour",
            "utilization": 0.92,
            "resets_at": 1792700000,
        }
    ) == {
        RATE_LIMIT_STATUS_LABEL: "allowed_warning",
        RATE_LIMIT_TYPE_LABEL: "five_hour",
        RATE_LIMIT_UTILIZATION_LABEL: "0.9200",
        RATE_LIMIT_RESETS_AT_LABEL: "1792700000",
    }
    assert rate_limit_label_updates({"status": "allowed"}) == {
        RATE_LIMIT_STATUS_LABEL: "allowed",
        RATE_LIMIT_TYPE_LABEL: "",
        RATE_LIMIT_UTILIZATION_LABEL: "",
        RATE_LIMIT_RESETS_AT_LABEL: "",
    }
    assert rate_limit_label_updates({"status": "bogus"}) is None


@pytest.mark.asyncio
async def test_a_store_failure_does_not_break_the_relay() -> None:
    store = Mock(spec=ConversationStore)
    store.set_labels.side_effect = RuntimeError("db down")
    await persist_rate_limit_labels("conv_1", {"status": "rejected"}, store)
    store.set_labels.assert_called_once()


@pytest.mark.asyncio
async def test_relay_persists_the_rate_limit_state_as_labels() -> None:
    body = (
        'data: {"type":"session.heartbeat"}\n\n'
        'data: {"type":"session.rate_limit","conversation_id":"conv_rl",'
        '"status":"allowed_warning","rate_limit_type":"five_hour",'
        '"utilization":0.95,"resets_at":1792700000}\n\n'
        "data: [DONE]\n\n"
    )
    store = Mock(spec=ConversationStore)
    async with httpx.AsyncClient(
        base_url="http://runner",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)),
    ) as client:
        await orchestration._relay_runner_stream_once("conv_rl", client, store)
    store.set_labels.assert_called_once_with(
        "conv_rl",
        {
            RATE_LIMIT_STATUS_LABEL: "allowed_warning",
            RATE_LIMIT_TYPE_LABEL: "five_hour",
            RATE_LIMIT_UTILIZATION_LABEL: "0.9500",
            RATE_LIMIT_RESETS_AT_LABEL: "1792700000",
        },
    )
