"""The provider-neutral LLM wrapper.

No network here. The one piece of real logic in the client is how request
options are assembled, and that is what these tests pin down; the round trip
itself was verified against the live provider in Step 0.
"""

import pytest
from pydantic import BaseModel

from src.infrastructure.llm.client import LLM, LLMResult, chat_options
from src.infrastructure.llm.exceptions import LLMError, LLMParsingError
from tests.fakes import BrokenLLM, FakeLLM


class Answer(BaseModel):
    category: str
    confidence: float


def test_unset_sampling_options_are_not_sent() -> None:
    """Several current models reject `temperature` with a 400."""
    assert chat_options(api_key="key", timeout_s=30.0) == {"api_key": "key", "timeout": 30.0}


def test_set_sampling_options_are_sent() -> None:
    options = chat_options(api_key="key", timeout_s=30.0, temperature=0.0, max_tokens=2048)

    assert options["temperature"] == 0.0
    assert options["max_tokens"] == 2048


def test_provider_specific_options_pass_through() -> None:
    """One field carries every provider's own knobs, so the client needs no branches."""
    options = chat_options(api_key="key", timeout_s=30.0, extra={"reasoning_effort": "medium"})

    assert options["reasoning_effort"] == "medium"


def test_an_explicit_argument_wins_over_the_same_key_in_extra() -> None:
    options = chat_options(api_key="key", timeout_s=30.0, temperature=0.5, extra={"temperature": 0.9})

    assert options["temperature"] == 0.5


async def test_fake_llm_satisfies_the_protocol() -> None:
    fake: LLM = FakeLLM(Answer(category="NEW_RFQ", confidence=0.9))
    result = await fake.invoke([("human", "hello")], Answer)

    assert isinstance(result, LLMResult)
    assert result.value.category == "NEW_RFQ"
    assert result.input_tokens == 100


async def test_fake_llm_records_the_prompt() -> None:
    fake = FakeLLM(Answer(category="INTERNAL", confidence=0.5))
    await fake.invoke([("system", "rules"), ("human", "body")], Answer)

    assert fake.calls == [[("system", "rules"), ("human", "body")]]


async def test_fake_llm_rejects_a_mismatched_schema() -> None:
    class Other(BaseModel):
        value: int

    fake = FakeLLM(Answer(category="X", confidence=0.1))
    with pytest.raises(TypeError):
        await fake.invoke([("human", "hi")], Other)


async def test_broken_llm_propagates_its_error() -> None:
    broken = BrokenLLM(LLMParsingError("Answer", "bad json"))
    with pytest.raises(LLMError):
        await broken.invoke([("human", "hi")], Answer)


def test_parsing_error_message_names_the_schema() -> None:
    error = LLMParsingError("LLMClassification", "missing field 'category'")
    assert "LLMClassification" in str(error)
