from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All external configuration, loaded once from the environment.

    Nothing tunable belongs in the code. What an operator edits without a
    release - Outlook label names, fast-path thresholds - lives in
    REGISTRIES_PATH instead.
    """

    PROJECT_NAME: str = "rfq-workflow-agent"
    LOG_LEVEL: str = "INFO"

    # --- LLM -----------------------------------------------------------------
    # Switching provider is these three lines and nothing else.
    LLM_PROVIDER: str
    LLM_MODEL: str
    LLM_API_KEY: SecretStr
    LLM_TIMEOUT_S: float = 30.0
    LLM_MAX_RETRIES: int = 3
    LLM_STRUCTURED_OUTPUT_METHOD: str = "json_schema"
    # Left unset by default: some models reject them outright.
    LLM_TEMPERATURE: float | None = None
    LLM_MAX_TOKENS: int | None = None
    # Provider-specific knobs, passed through verbatim.
    # OpenAI: {"reasoning_effort": "medium"} · Anthropic: {"thinking": {"type": "adaptive"}}
    LLM_EXTRA_OPTIONS: dict[str, Any] = {}

    # --- Forwarding ------------------------------------------------------------
    # Off by default. This is the first action that sends mail to real people, so
    # switching it on is a deliberate step taken after watching the labels.
    # Needs the Mail.Send application permission with admin consent.
    FORWARD_ENABLED: bool = False

    # Where each regional desk's RFQs go. Real addresses, so they live here and
    # never in the committed registries file. Adding a desk is one line here,
    # one in `region_mailboxes`, and a block in registries.yaml.
    UAE_MAILBOX: str = ""
    SG_MAILBOX: str = ""

    # Who is copied on a forward, comma separated. These addresses receive the
    # customer's email in full, so keep the list to people who should see it.
    UAE_CC: str = ""
    SG_CC: str = ""

    # --- Classification pipeline ---------------------------------------------
    FAST_PATH_ENABLED: bool = True
    # The whole thread goes to the model; this only stops a runaway chain.
    MAX_QUOTED_MESSAGES_IN_PROMPT: int = 10
    MIN_LATEST_CHARS_FOR_VALID_SPLIT: int = 15

    # --- Confidence thresholds -----------------------------------------------
    # Starting heuristics: self-reported confidence is not a probability, so
    # these must be recalibrated on real traffic.
    CONFIDENCE_AUTO_THRESHOLD: float = 0.85
    CONFIDENCE_REVIEW_THRESHOLD: float = 0.60

    # --- Prompt and registries -----------------------------------------------
    PROMPT_VERSION: str = "v1.0.0"
    REGISTRIES_PATH: Path = Path("config/registries.yaml")

    # --- Decision log --------------------------------------------------------
    PERSIST_DECISIONS: bool = True
    DECISIONS_LOG_PATH: Path = Path("data/decisions.jsonl")
    # Covers the body only: the model's evidence quotes the email, so the
    # journal is confidential either way.
    LOG_EMAIL_BODIES: bool = False

    # --- Microsoft Entra ID / Graph ------------------------------------------
    # False: serve HTTP only, do not watch the mailbox. The classify endpoint
    # works either way; this only decides whether a Graph subscription is opened.
    OUTLOOK_ENABLED: bool
    APPLICATION_CLIENT_ID: str
    DIRECTORY_TENANT_ID: str
    CLIENT_SECRET_VALUE: str
    MAILBOX_ADDRESS: str
    GRAPH_BASE_URL: str = "https://graph.microsoft.com/v1.0"
    ENTRA_LOGIN_URL: str = "https://login.microsoftonline.com"
    HTTP_TIMEOUT_SECONDS: float = 30.0

    # Public URL Graph pushes notifications to (ngrok during development).
    NGROK_URL: str
    # Shared secret echoed back by Graph in every notification.
    WEBHOOK_CLIENT_STATE: str
    # Subscriptions expire; renewal happens before that.
    SUBSCRIPTION_EXPIRATION_MINUTES: int = 60
    SUBSCRIPTION_RENEWAL_MARGIN_MINUTES: int = 15

    model_config = SettingsConfigDict(
        case_sensitive=True,
        frozen=True,
        env_file=".env",
        extra="ignore",
    )

    @property
    def authority_url(self) -> str:
        return f"{self.ENTRA_LOGIN_URL}/{self.DIRECTORY_TENANT_ID}"

    @property
    def notification_url(self) -> str:
        return f"{self.NGROK_URL.rstrip('/')}{'/webhooks/outlook'}"

    @property
    def region_mailboxes(self) -> dict[str, str]:
        """Region key in registries.yaml -> the address its RFQs are forwarded to."""
        return {"uae": self.UAE_MAILBOX, "sg": self.SG_MAILBOX}

    @property
    def region_cc(self) -> dict[str, list[str]]:
        """Region key in registries.yaml -> who is copied on its forwards."""
        return {"uae": _addresses(self.UAE_CC), "sg": _addresses(self.SG_CC)}


def _addresses(value: str) -> list[str]:
    """Split a comma-separated setting, tolerating spaces and a trailing comma.

    The field stays a plain string because pydantic-settings parses a `list[str]`
    as JSON, which "a@x.com,b@y.com" is not.
    """
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # ty: ignore
