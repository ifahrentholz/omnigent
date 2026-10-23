"""The claude-sdk executor maps Claude's ``RateLimitEvent`` to ``RateLimitChanged``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from omnigent.inner.claude_sdk_executor import _rate_limit_changed
from omnigent.inner.executor import RateLimitChanged


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
        status="allowed_warning",
        rate_limit_type="five_hour",
        utilization=0.92,
        resets_at=1792700000,
    )
    assert _rate_limit_changed(_Sdk, object()) is None, "other messages fall through"
    assert _rate_limit_changed(object(), _RateLimitEvent(_Info("allowed"))) is None, (
        "an SDK without the class yields nothing"
    )


def test_executor_maps_the_real_sdk_class() -> None:
    sdk = pytest.importorskip("claude_agent_sdk")
    if not hasattr(sdk, "RateLimitEvent"):
        pytest.skip("this claude_agent_sdk has no RateLimitEvent")
    message = sdk.RateLimitEvent(
        rate_limit_info=sdk.RateLimitInfo(
            status="allowed_warning",
            resets_at=1792700000,
            rate_limit_type="five_hour",
            utilization=0.93,
        ),
        uuid="u1",
        session_id="s1",
    )
    assert _rate_limit_changed(sdk, message) == RateLimitChanged(
        status="allowed_warning",
        rate_limit_type="five_hour",
        utilization=0.93,
        resets_at=1792700000,
    )
