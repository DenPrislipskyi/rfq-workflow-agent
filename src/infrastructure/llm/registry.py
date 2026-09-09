"""Two models, because there are two kinds of question.

**Text** reads the email itself - is this an RFQ, what does the subject say.
**Documents** reads what was attached to it - a scanned requisition, a photo of
a nameplate, a spreadsheet whose columns need naming. The second job needs a
model that can see at all, and often a stronger one; the first has run on a
cheap model since Phase 1 and should not be dragged along.

Both come from the environment, next to `LLM_MODEL` where model choice already
lived. Each `LLM_DOCUMENT_*` value falls back to its plain counterpart, so a
deployment that wants one model for everything sets nothing extra and gets
exactly the behaviour it had before this file existed.

Callers get back the `LLM` protocol and never learn which provider answered.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from src.infrastructure.llm.client import LLM, StructuredLLM

logger = logging.getLogger(__name__)

TEXT = "text"
DOCUMENTS = "documents"


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One fully resolved model choice."""

    provider: str
    model: str
    api_key: str = field(repr=False)
    timeout_s: float = 30.0
    max_retries: int = 3
    structured_output_method: str = "json_schema"
    temperature: float | None = None
    max_tokens: int | None = None
    extra_options: dict[str, object] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Identity for caching, with the secret left out.

        A JSON dump rather than the dataclass itself, because `extra_options`
        holds nested dicts and is not hashable.
        """
        return json.dumps(
            {
                "provider": self.provider,
                "model": self.model,
                "timeout_s": self.timeout_s,
                "max_retries": self.max_retries,
                "method": self.structured_output_method,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "extra": self.extra_options,
            },
            sort_keys=True,
            default=str,
        )

    def __str__(self) -> str:
        return f"{self.model} via {self.provider}"


type ClientFactory = Callable[[ModelSpec], LLM]


def build_client(spec: ModelSpec) -> LLM:
    """The real factory. Injected, so tests never reach a provider."""
    return StructuredLLM(
        provider=spec.provider,
        model=spec.model,
        api_key=spec.api_key,
        timeout_s=spec.timeout_s,
        max_retries=spec.max_retries,
        structured_output_method=spec.structured_output_method,
        temperature=spec.temperature,
        max_tokens=spec.max_tokens,
        extra_options=spec.extra_options,
    )


class UnknownProviderError(ValueError):
    """A model was asked for from a provider this deployment holds no key for."""

    def __init__(self, provider: str, known: list[str]) -> None:
        super().__init__(
            f"No API key configured for provider {provider!r}. "
            f"Configured: {', '.join(known) or 'none'}"
        )
        self.provider = provider


class LLMRegistry:
    """The two models, built on first use and kept.

    `init_chat_model` opens a connection pool, so rebuilding a client per email
    would throw that away. Caching is by resolved spec rather than by purpose:
    when both purposes name the same model - the default - they share one client.
    """

    def __init__(
        self,
        *,
        text: ModelSpec,
        documents: ModelSpec,
        factory: ClientFactory = build_client,
    ) -> None:
        self._specs = {TEXT: text, DOCUMENTS: documents}
        self._factory = factory
        self._clients: dict[str, LLM] = {}

    @property
    def text(self) -> LLM:
        """Reads the email: subject, body, thread."""
        return self._client(self._specs[TEXT])

    @property
    def documents(self) -> LLM:
        """Reads the attachments: scans, photos, spreadsheets."""
        return self._client(self._specs[DOCUMENTS])

    def override(self, *, provider: str | None = None, model: str | None = None) -> LLM:
        """One caller's own choice, for evals and A/B against the same corpus.

        Everything but the provider and the model stays as configured: a
        comparison in which the timeout also moved is not a comparison.
        """
        spec = self._specs[TEXT]
        provider = provider or spec.provider
        return self._client(
            replace(
                spec,
                provider=provider,
                model=model or spec.model,
                api_key=self._api_key_for(provider),
            )
        )

    def describe(self) -> dict[str, str]:
        """Purpose to model, for two lines in the startup log."""
        return {purpose: str(spec) for purpose, spec in self._specs.items()}

    def _client(self, spec: ModelSpec) -> LLM:
        if spec.key not in self._clients:
            logger.debug("Building client for %s", spec)
            self._clients[spec.key] = self._factory(spec)
        return self._clients[spec.key]

    def _api_key_for(self, provider: str) -> str:
        """The key we hold for this provider, from whichever purpose configured it."""
        for spec in self._specs.values():
            if spec.provider == provider:
                return spec.api_key
        raise UnknownProviderError(
            provider, sorted({spec.provider for spec in self._specs.values()})
        )
