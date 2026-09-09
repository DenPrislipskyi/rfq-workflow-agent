"""B1: two models, one for the email text and one for the attachments.

No provider is reached anywhere here - the registry takes its client factory as
an argument, so every test asserts on the `ModelSpec` that would have been sent
to it.
"""

import pytest

from src.core.lifespan import build_llm_registry
from src.infrastructure.llm.registry import (
    LLMRegistry,
    ModelSpec,
    UnknownProviderError,
)
from tests.fakes import fake_settings


class RecordingFactory:
    """Stands in for `build_client` and remembers every spec it was handed."""

    def __init__(self) -> None:
        self.built: list[ModelSpec] = []

    def __call__(self, spec: ModelSpec) -> object:
        self.built.append(spec)
        return object()


def spec(**overrides: object) -> ModelSpec:
    return ModelSpec(**{"provider": "openai", "model": "text-model", "api_key": "key"} | overrides)


def registry(
    documents: ModelSpec | None = None, *, factory: RecordingFactory | None = None
) -> LLMRegistry:
    text = spec()
    return LLMRegistry(
        text=text,
        documents=documents or text,
        factory=factory or RecordingFactory(),
    )


# --- the two purposes -----------------------------------------------------


def test_one_model_configured_means_both_purposes_use_it():
    """The default deployment sets nothing extra and behaves as it always did."""
    factory = RecordingFactory()
    resolved = registry(factory=factory)

    assert resolved.text is resolved.documents
    assert len(factory.built) == 1, "one spec, one client"


def test_a_separate_document_model_is_used_for_attachments_only():
    factory = RecordingFactory()
    resolved = registry(
        spec(provider="anthropic", model="seeing-model", api_key="second"),
        factory=factory,
    )

    resolved.text
    resolved.documents

    assert [built.model for built in factory.built] == ["text-model", "seeing-model"]
    assert [built.provider for built in factory.built] == ["openai", "anthropic"]


def test_each_purpose_gets_its_own_api_key():
    factory = RecordingFactory()
    resolved = registry(spec(provider="anthropic", api_key="second"), factory=factory)

    resolved.text
    resolved.documents

    assert [built.api_key for built in factory.built] == ["key", "second"]


def test_a_client_is_built_once_and_reused():
    """`init_chat_model` opens a connection pool; rebuilding it per email
    would throw that away."""
    factory = RecordingFactory()
    resolved = registry(factory=factory)

    assert resolved.text is resolved.text
    assert len(factory.built) == 1


def test_the_api_key_is_kept_out_of_the_spec_repr():
    """Specs reach the debug log; secrets must not."""
    assert "key" not in repr(spec(api_key="super-secret"))
    assert "super-secret" not in spec(api_key="super-secret").key


# --- per-request override -------------------------------------------------


def test_an_override_changes_the_model_and_nothing_else():
    """A comparison in which the timeout also moved is not a comparison."""
    factory = RecordingFactory()
    registry(factory=factory).override(model="challenger")

    assert factory.built[0].model == "challenger"
    assert factory.built[0].provider == "openai"
    assert factory.built[0].timeout_s == 30.0


def test_an_override_may_name_the_document_provider():
    """Its key is already configured, so the comparison can cross providers."""
    factory = RecordingFactory()
    resolved = registry(spec(provider="anthropic", api_key="second"), factory=factory)

    resolved.override(provider="anthropic", model="challenger")

    assert factory.built[0].api_key == "second"


def test_an_override_naming_an_unconfigured_provider_is_refused():
    with pytest.raises(UnknownProviderError) as error:
        registry().override(provider="mystery", model="x")

    assert "mystery" in str(error.value)


# --- wiring from settings -------------------------------------------------


def test_the_document_model_defaults_to_the_text_one():
    settings = fake_settings(LLM_MODEL="only-model")

    assert settings.document_model == "only-model"
    assert settings.document_provider == settings.LLM_PROVIDER
    assert settings.document_api_key == "test-key"


def test_only_the_document_model_may_differ():
    """The common case: same provider and key, a stronger model for scans."""
    settings = fake_settings(LLM_DOCUMENT_MODEL="seeing-model")

    assert settings.document_model == "seeing-model"
    assert settings.document_provider == settings.LLM_PROVIDER
    assert settings.document_api_key == "test-key"


def test_a_blank_document_key_counts_as_unset():
    """`.env.example` ships the line empty; filling in only the model must work."""
    settings = fake_settings(LLM_DOCUMENT_MODEL="seeing-model", LLM_DOCUMENT_API_KEY="")

    assert settings.document_api_key == "test-key"


def test_another_provider_without_its_key_is_refused():
    """Falling back to the wrong provider's key would only be a 401 later."""
    settings = fake_settings(LLM_DOCUMENT_PROVIDER="anthropic")

    with pytest.raises(ValueError, match="LLM_DOCUMENT_API_KEY"):
        _ = settings.document_api_key


def test_the_registry_built_from_settings_carries_both_models():
    settings = fake_settings(
        LLM_MODEL="text-model",
        LLM_DOCUMENT_MODEL="seeing-model",
        LLM_TIMEOUT_S=12.0,
    )

    described = build_llm_registry(settings).describe()

    assert described == {
        "text": "text-model via fake",
        "documents": "seeing-model via fake",
    }


def test_both_purposes_share_the_service_wide_options():
    """Timeout and retries belong to this service, not to either question."""
    factory = RecordingFactory()
    settings = fake_settings(LLM_DOCUMENT_MODEL="seeing-model", LLM_TIMEOUT_S=12.0)

    built = build_llm_registry(settings)
    built._factory = factory  # noqa: SLF001 - the wiring is what is under test
    built.text
    built.documents

    assert {spec.timeout_s for spec in factory.built} == {12.0}
