"""Claude's rate-limit warnings reach the session: executor → SSE → durable labels."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from omnigent.inner.claude_sdk_executor import _rate_limit_changed
from omnigent.inner.executor import RateLimitChanged
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import TurnContext
from omnigent.server.rate_limit_labels import (
    RATE_LIMIT_RESETS_AT_LABEL,
    RATE_LIMIT_STATUS_LABEL,
    RATE_LIMIT_TYPE_LABEL,
    RATE_LIMIT_UTILIZATION_LABEL,
    persist_rate_limit_labels,
    rate_limit_label_updates,
)
from omnigent.server.routes._sessions import orchestration
from omnigent.server.schemas import SessionRateLimitEvent
from omnigent.stores.conversation_store import ConversationStore


@dataclass
class _Info:
    status: str
    rate_limit_type: str | None = None
    utilization: float | None = None
    resets_at: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class _RateLimitEvent:
    rate_limit_info: _Info
    uuid: str = "u1"
    session_id: str = "s1"


class _Sdk:
    RateLimitEvent = _RateLimitEvent


def test_executor_maps_the_sdk_rate_limit_event() -> None:
    event = _rate_limit_changed(
        _Sdk, _RateLimitEvent(_Info("allowed_warning", "five_hour", 0.92, 1792700000))
    )
    assert event == RateLimitChanged(
        status="allowed_warning", rate_limit_type="five_hour", utilization=0.92, resets_at=1792700000
    )
    assert _rate_limit_changed(_Sdk, object()) is None, "other messages fall through"
    assert _rate_limit_changed(object(), _RateLimitEvent(_Info("allowed"))) is None, (
        "an SDK without the class yields nothing"
    )


@pytest.mark.asyncio
async def test_adapter_emits_session_rate_limit_without_marking_progress() -> None:
    adapter = ExecutorAdapter.__new__(ExecutorAdapter)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    ctx = TurnContext("resp_1", queue, asyncio.Event(), session_id="conv_1")
    adapter._translate_event(
        RateLimitChanged(status="allowed_warning", rate_limit_type="seven_day", utilization=0.9),
        ctx,
    )
    emitted = queue.get_nowait()
    assert isinstance(emitted, SessionRateLimitEvent)
    assert emitted.model_dump(exclude_none=True) == {
        "type": "session.rate_limit",
        "conversation_id": "conv_1",
        "status": "allowed_warning",
        "rate_limit_type": "seven_day",
        "utilization": 0.9,
    }
    # A rate-limit report is no output: a crashed turn may still be replayed.
    assert ctx._has_progress is False


def test_label_updates_clear_absent_fields_and_skip_unknown_status() -> None:
    assert rate_limit_label_updates(
        {"status": "allowed_warning", "rate_limit_type": "five_hour", "utilization": 0.92, "resets_at": 1792700000}
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
        'data: {"type":"session.rate_limit","conversation_id":"conv_rl","status":"allowed_warning",'
        '"rate_limit_type":"five_hour","utilization":0.95,"resets_at":1792700000}\n\n'
        "data: [DONE]\n\n"
    )
    store = Mock(spec=ConversationStore)
    async with httpx.AsyncClient(
        base_url="http://runner", transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
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


def test_executor_maps_the_real_sdk_class() -> None:
    sdk = pytest.importorskip("claude_agent_sdk")
    if not hasattr(sdk, "RateLimitEvent"):
        pytest.skip("this claude_agent_sdk has no RateLimitEvent")
    message = sdk.RateLimitEvent(
        rate_limit_info=sdk.RateLimitInfo(status="allowed_warning", resets_at=1792700000, rate_limit_type="five_hour", utilization=0.93),
        uuid="u1",
        session_id="s1",
    )
    assert _rate_limit_changed(sdk, message) == RateLimitChanged(
        status="allowed_warning", rate_limit_type="five_hour", utilization=0.93, resets_at=1792700000
    )
