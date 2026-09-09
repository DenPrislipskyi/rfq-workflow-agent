"""Test doubles.

`FakeLLM` lives here rather than in `src` on purpose: it is only ever used by
tests, and shipping it inside the application invites someone to wire it up by
accident. It satisfies the same `LLM` protocol as `StructuredLLM`.
"""

from pydantic import BaseModel

from src.core.config import Settings
from src.infrastructure.llm.client import LLMResult, Messages


class FakeLLM:
    """Returns a prepared answer and records what it was asked."""

    def __init__(self, answer: BaseModel, *, latency_ms: int = 5) -> None:
        self.answer = answer
        self.latency_ms = latency_ms
        self.calls: list[Messages] = []

    async def invoke[T: BaseModel](self, messages: Messages, schema: type[T]) -> LLMResult[T]:
        self.calls.append(messages)
        if not isinstance(self.answer, schema):
            raise TypeError(f"FakeLLM was set up with {type(self.answer).__name__}, not {schema.__name__}")
        return LLMResult(
            value=self.answer,
            model="fake",
            latency_ms=self.latency_ms,
            input_tokens=100,
            output_tokens=20,
        )


class BrokenLLM:
    """Raises whatever it was given - for testing the error paths."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def invoke[T: BaseModel](self, messages: Messages, schema: type[T]) -> LLMResult[T]:
        raise self.error


def fake_settings(**overrides: object) -> Settings:
    """Settings with every required field filled in.

    `_env_file=None` keeps the developer's real `.env` out of the tests: the
    pipeline's behaviour must not depend on which model happens to be configured.
    """
    defaults: dict[str, object] = {
        # Off by default: a test must never open a Graph subscription.
        "OUTLOOK_ENABLED": False,
        # Off for the same reason: a test must never leave a 13 MB workbook in
        # the working tree. The tests that want one point it at `tmp_path`.
        "WORKBOOKS_ENABLED": False,
        "LLM_PROVIDER": "fake",
        "LLM_MODEL": "fake-model",
        "LLM_API_KEY": "test-key",
        "APPLICATION_CLIENT_ID": "client-id",
        "DIRECTORY_TENANT_ID": "tenant-id",
        "CLIENT_SECRET_VALUE": "secret",
        "MAILBOX_ADDRESS": "supply@our-company.com",
        "NGROK_URL": "https://example.invalid",
        "WEBHOOK_CLIENT_STATE": "state",
    }
    return Settings(_env_file=None, **(defaults | overrides))  # ty: ignore
