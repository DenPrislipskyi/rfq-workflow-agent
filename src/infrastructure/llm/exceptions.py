class LLMError(Exception):
    """Base error for every LLM interaction."""


class LLMParsingError(LLMError):
    """The model answered, but the answer did not fit the requested schema."""

    def __init__(self, schema_name: str, detail: str) -> None:
        self.schema_name = schema_name
        self.detail = detail
        super().__init__(f"Could not parse the response as {schema_name}: {detail}")


class LLMCallError(LLMError):
    """The provider could not be reached, or refused the request."""

    def __init__(self, model: str, cause: BaseException) -> None:
        self.model = model
        super().__init__(f"Call to {model} failed: {type(cause).__name__}: {cause}")


class LLMTimeoutError(LLMError):
    """The provider did not answer in time."""

    def __init__(self, model: str, timeout_s: float) -> None:
        self.model = model
        super().__init__(f"Call to {model} timed out after {timeout_s}s")
