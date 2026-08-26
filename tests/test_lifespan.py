"""Wiring at startup.

Only one branch here is worth pinning: whether the mailbox subscription is
opened. Everything else in `lifespan` is construction, and the tests for each
piece live next to that piece.
"""

import pytest
from fastapi import FastAPI

from src.core import lifespan as lifespan_module
from tests.fakes import fake_settings


@pytest.fixture
def no_subscription_calls(monkeypatch):
    """Record start/stop instead of opening a real Graph subscription."""
    calls: list[str] = []
    monkeypatch.setattr(
        lifespan_module.SubscriptionManager,
        "start",
        lambda self: _record(calls, "start"),
    )
    monkeypatch.setattr(
        lifespan_module.SubscriptionManager,
        "stop",
        lambda self: _record(calls, "stop"),
    )
    return calls


async def _record(calls: list[str], name: str) -> None:
    calls.append(name)


def settings(*, outlook: bool):
    """Real Settings, a provider name `init_chat_model` accepts, no network."""
    return fake_settings(
        OUTLOOK_ENABLED=outlook,
        LLM_PROVIDER="openai",
        LLM_MODEL="gpt-4o-mini",
        PERSIST_DECISIONS=False,
    )


async def run_lifespan(monkeypatch, *, outlook: bool) -> None:
    monkeypatch.setattr(lifespan_module, "get_settings", lambda: settings(outlook=outlook))
    async with lifespan_module.lifespan(FastAPI()) as state:
        assert state["triage"] is not None
        assert state["subscription_manager"] is not None


async def test_the_mailbox_is_not_watched_when_outlook_is_off(
    monkeypatch, no_subscription_calls
) -> None:
    """Local development: no ngrok, no admin consent, no retries in the log."""
    await run_lifespan(monkeypatch, outlook=False)
    assert no_subscription_calls == []


async def test_the_mailbox_is_watched_when_outlook_is_on(
    monkeypatch, no_subscription_calls
) -> None:
    await run_lifespan(monkeypatch, outlook=True)
    assert no_subscription_calls == ["start", "stop"]


async def test_triage_is_built_either_way(monkeypatch, no_subscription_calls) -> None:
    """The endpoint must not depend on the mailbox integration being available."""
    for outlook in (True, False):
        await run_lifespan(monkeypatch, outlook=outlook)
