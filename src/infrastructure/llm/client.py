"""Provider-neutral LLM access.

Callers hand over messages and a Pydantic schema and get a validated instance
back, so this layer knows neither the provider nor the task. Switching provider
is three environment values - LLM_PROVIDER, LLM_MODEL, LLM_API_KEY - plus the
matching `langchain-*` package.
"""

import logging
import time
from base64 import b64encode
from dataclasses import dataclass
from typing import Any, Protocol

from langchain.chat_models import init_chat_model
from langchain_core.messages.content import create_image_block
from langchain_core.runnables import Runnable
from pydantic import BaseModel

from src.core.logging import tally
from src.infrastructure.llm.exceptions import LLMCallError, LLMParsingError, LLMTimeoutError

logger = logging.getLogger(__name__)

# A piece of one message: text, or an image built by `image_block`.
type ContentBlock = dict[str, Any]
# Plain text covers most prompts; a list of blocks is what carries a scan or a
# photo alongside the words describing it.
type MessageContent = str | list[ContentBlock]
# Role/content pairs, e.g. ("system", "..."), ("human", "..."), ("ai", "...").
type Messages = list[tuple[str, MessageContent]]


def image_block(data: bytes, media_type: str) -> ContentBlock:
    """One image, ready to sit beside text in a human message.

    Base64 rather than a URL: the bytes came out of an email attachment and are
    never served from anywhere. LangChain's factory is used instead of a hand
    written dict so the block keeps whatever shape the installed version expects.
    """
    return create_image_block(
        base64=b64encode(data).decode("ascii"), mime_type=media_type
    )


@dataclass(frozen=True, slots=True)
class LLMResult[T]:
    """A parsed answer plus what it cost to get it."""

    value: T
    model: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLM(Protocol):
    """What the services layer needs from a model. `FakeLLM` implements it too."""

    async def invoke[T: BaseModel](self, messages: Messages, schema: type[T]) -> LLMResult[T]: ...


def chat_options(
    *,
    api_key: str,
    timeout_s: float,
    temperature: float | None = None,
    max_tokens: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the keyword arguments for `init_chat_model`.

    `temperature` and `max_tokens` are sent only when configured, because
    several current models reject them outright. `extra` carries provider knobs
    such as `reasoning_effort`, and an explicit argument beats the same key there.
    """
    options: dict[str, Any] = {"api_key": api_key, "timeout": timeout_s, **(extra or {})}
    if temperature is not None:
        options["temperature"] = temperature
    if max_tokens is not None:
        options["max_tokens"] = max_tokens
    return options


class StructuredLLM:
    """An LLM that always answers with a validated Pydantic object."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str,
        timeout_s: float,
        max_retries: int,
        structured_output_method: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_options: dict[str, Any] | None = None,
    ) -> None:
        self._model = model
        self._timeout_s = timeout_s
        self._method = structured_output_method
        self._max_retries = max_retries
        options = chat_options(
            api_key=api_key,
            timeout_s=timeout_s,
            temperature=temperature,
            max_tokens=max_tokens,
            extra=extra_options,
        )
        self._chat = init_chat_model(model, model_provider=provider, **options)

    async def invoke[T: BaseModel](self, messages: Messages, schema: type[T]) -> LLMResult[T]:
        """Ask the model and return an instance of `schema`, never raw text."""
        started = time.perf_counter()
        answer = await self._ask(self._runnable(schema), messages)
        latency_ms = round((time.perf_counter() - started) * 1000)

        if answer["parsing_error"] is not None:
            raise LLMParsingError(schema.__name__, str(answer["parsing_error"]))

        # Not every provider reports token usage.
        usage = getattr(answer["raw"], "usage_metadata", None) or {}
        result = LLMResult(
            value=answer["parsed"],
            model=self._model,
            latency_ms=latency_ms,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
        self._account_for(schema, result)
        return result

    def _account_for(self, schema: type[BaseModel], result: LLMResult[Any]) -> None:
        """One line per call, and one number per email.

        The only place in the service that knows a model was asked anything, so
        it is the only place that has to say so. DEBUG rather than INFO because
        an RFQ makes up to eight of these and the closing line already carries
        their total - this level is for the run where one call has to be found.
        """
        logger.debug(
            "Asked %s of %s: %d ms, %s->%s tok",
            schema.__name__,
            result.model,
            result.latency_ms,
            result.input_tokens if result.input_tokens is not None else "?",
            result.output_tokens if result.output_tokens is not None else "?",
        )
        if (spent := tally()) is not None:
            spent.record(
                ms=result.latency_ms,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )

    def _runnable(self, schema: type[BaseModel]) -> Runnable:
        """The call chain for one schema.

        Order matters: `with_structured_output` belongs to the chat model, and
        `with_retry` returns a plain Runnable that no longer has it. Rebuilding
        it per call costs under a millisecond, so it is not cached.
        """
        return self._chat.with_structured_output(
            schema, method=self._method, include_raw=True
        ).with_retry(stop_after_attempt=self._max_retries)

    async def _ask(self, runnable: Runnable, messages: Messages) -> dict[str, Any]:
        """Run the call, letting nothing but `LLMError` out.

        Returns LangChain's `include_raw` envelope - `parsed`, `raw`,
        `parsing_error` - and translating provider exceptions here is what keeps
        `openai` and `anthropic` out of every layer above.
        """
        try:
            return await runnable.ainvoke(messages)
        except Exception as error:
            if _is_timeout(error):
                raise LLMTimeoutError(self._model, self._timeout_s) from error
            raise LLMCallError(self._model, error) from error


def _is_timeout(error: BaseException) -> bool:
    """Providers share no timeout base class, so the class name is the signal.

    `openai.APITimeoutError` descends from `APIConnectionError`, not from
    `TimeoutError`; anthropic and httpx each have their own.
    """
    return isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower()
