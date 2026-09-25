"""Relaunching the runner a sub-agent shares with its host-bound ancestor."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.server.routes._sessions import orchestration


class _Store:
    """Minimal conversation store: ``get_conversation`` by id."""

    def __init__(self, *conversations: SimpleNamespace) -> None:
        self._by_id = {conv.id: conv for conv in conversations}

    def get_conversation(self, conversation_id: str) -> SimpleNamespace | None:
        return self._by_id.get(conversation_id)


def _conv(conv_id: str, *, parent: str | None = None, host: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=conv_id, parent_conversation_id=parent, host_id=host)


@pytest.fixture
def runner() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(204)),
        base_url="http://runner",
    )


async def test_wakes_the_host_ancestor_initializes_it_and_heals_the_child(
    monkeypatch: pytest.MonkeyPatch,
    runner: httpx.AsyncClient,
) -> None:
    """The orchestrator's runner is relaunched, re-initialized idle, then the child heals."""
    root = _conv("root", host="host_1")
    child = _conv("child", parent="root")
    store = _Store(root, child)
    calls: list[tuple[str, Any]] = []

    async def _connect(**kwargs: Any) -> tuple[httpx.AsyncClient, SimpleNamespace]:
        calls.append(("connect", kwargs["session_id"]))
        return runner, kwargs["conv"]

    async def _init(session_id: str, *_args: Any, **kwargs: Any) -> bool:
        calls.append(("init", (session_id, kwargs["suppress_recovery_turn"])))
        return True

    async def _heal(child_conv: Any, *_args: Any) -> httpx.AsyncClient:
        calls.append(("heal", child_conv.id))
        return runner

    monkeypatch.setattr(orchestration, "ensure_runner_connected", _connect)
    monkeypatch.setattr(orchestration, "_ensure_runner_session_initialized", _init)
    monkeypatch.setattr(orchestration, "_heal_subagent_runner_binding_via_parent", _heal)

    client = await orchestration._wake_subagent_host_runner(
        child,  # type: ignore[arg-type]
        SimpleNamespace(),
        store,  # type: ignore[arg-type]
        None,
    )

    assert client is runner
    assert calls == [("connect", "root"), ("init", ("root", True)), ("heal", "child")]


async def test_returns_none_without_a_host_bound_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tree with no host cannot be woken; the caller keeps its 503."""
    child = _conv("child", parent="root")
    store = _Store(_conv("root"), child)

    async def _connect(**_kwargs: Any) -> None:
        raise AssertionError("no host to wake")

    monkeypatch.setattr(orchestration, "ensure_runner_connected", _connect)

    assert (
        await orchestration._wake_subagent_host_runner(
            child,  # type: ignore[arg-type]
            SimpleNamespace(),
            store,  # type: ignore[arg-type]
            None,
        )
        is None
    )


async def test_returns_none_when_the_ancestor_runner_stays_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An offline host leaves the child unhealed."""
    root = _conv("root", host="host_1")
    child = _conv("child", parent="root")
    store = _Store(root, child)

    async def _connect(**kwargs: Any) -> tuple[None, SimpleNamespace]:
        return None, kwargs["conv"]

    async def _heal(*_args: Any) -> None:
        raise AssertionError("must not heal without a live runner")

    monkeypatch.setattr(orchestration, "ensure_runner_connected", _connect)
    monkeypatch.setattr(orchestration, "_heal_subagent_runner_binding_via_parent", _heal)

    assert (
        await orchestration._wake_subagent_host_runner(
            child,  # type: ignore[arg-type]
            SimpleNamespace(),
            store,  # type: ignore[arg-type]
            None,
        )
        is None
    )
