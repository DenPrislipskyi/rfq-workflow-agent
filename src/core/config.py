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
    # Third-party loggers are pinned separately, because `httpx` logs a line per
    # request and `pdfminer` several per page: at our own DEBUG they would bury
    # the lines somebody turned DEBUG on to read. Raise this to see them.
    LOG_LIBRARY_LEVEL: str = "WARNING"

    # --- LLM -----------------------------------------------------------------
    # Two models, because there are two kinds of question. This one reads the
    # email itself - is this an RFQ, what does the subject say. Switching
    # provider is these three lines and nothing else.
    LLM_PROVIDER: str
    LLM_MODEL: str
    LLM_API_KEY: SecretStr

    # And this one reads the attachments: a scanned requisition, a photo of a
    # nameplate, a spreadsheet whose columns need naming. It has to be able to
    # see, and is usually the stronger of the two.
    #
    # Each value falls back to its plain counterpart above, so leaving all three
    # unset means one model does everything - which is how the service ran
    # before attachments were read at all.
    LLM_DOCUMENT_PROVIDER: str = ""
    LLM_DOCUMENT_MODEL: str = ""
    LLM_DOCUMENT_API_KEY: SecretStr | None = None
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

    # --- RFQ extraction (Phase 2) --------------------------------------------
    # Read every RFQ's attachments and fill the template's fields. Costs a few
    # model calls per RFQ and sends nothing anywhere, so unlike FORWARD_ENABLED
    # it is on by default; switching it off leaves Phase 1 exactly as it was.
    EXTRACTION_ENABLED: bool = True
    # Read the attachments before deciding whether the email is an RFQ, and show
    # the verdict what was in them. The strongest evidence an email is an RFQ is
    # a requisition sitting in a file, and a body that says no more than "please
    # find attached" carries none of it.
    #
    # What it costs: a non-RFQ that arrives with files now pays one document
    # model call per file instead of nothing, which is what the fast path in
    # front of it exists to bound. A real RFQ costs the same either way - the
    # files were going to be read regardless.
    #
    # Reading lives in the extraction pipeline, so this has no effect while
    # EXTRACTION_ENABLED is off. Off, the verdict is taken from the text alone
    # and the files are read afterwards, as they were before.
    TRIAGE_READS_ATTACHMENTS: bool = True
    # The form the agent fills and sends: 14 KB, no macro, no master data - the
    # same shape the desk produces by hand. Made from a finished RFQ with
    # `src.tools.make_output_template`, and shipped in the image.
    RFQ_TEMPLATE_PATH: Path = Path("config/rfq_output_template.xlsx")
    # Where the filled copies are kept, one per RFQ, named after the decision
    # that produced it. Until the forward carries the file this folder is the
    # only way to see what came out.
    WORKBOOKS_ENABLED: bool = True
    WORKBOOKS_PATH: Path = Path("data/workbooks")
    # Attach the filled copy to the forward. Separate from FORWARD_ENABLED
    # because it is a separate risk: the file is ~12.5 MB against Exchange's
    # 35 MB default, so it can be switched off without stopping the forwards.
    # Needs Mail.ReadWrite on top of Mail.Send - a forward that carries a new
    # attachment has to exist as a draft first.
    FORWARD_WORKBOOK: bool = True
    # The zone `H10` and `H12` are written in. A spreadsheet cell carries no
    # zone of its own, so this is a decision rather than a detail: an email that
    # arrives at 16:33 in Singapore reads 08:33 on the form under UTC.
    RFQ_TIMEZONE: str = "UTC"
    # Write what could not be filled in, and why, into the block under the form.
    # Off leaves the form itself silent; the forwarded email and the journal say
    # it either way.
    RFQ_REMARKS: bool = True

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
    def document_provider(self) -> str:
        return self.LLM_DOCUMENT_PROVIDER or self.LLM_PROVIDER

    @property
    def document_model(self) -> str:
        return self.LLM_DOCUMENT_MODEL or self.LLM_MODEL

    @property
    def document_api_key(self) -> str:
        """The document model's key, or the main one when it is the same provider.

        An empty value counts as unset, not as an empty key: `.env.example`
        ships the line blank, and a deployment that copies it and fills in only
        the model would otherwise authenticate with "" and get a 401 several
        layers down.

        The fallback stops at the provider boundary for the same reason - one
        provider's key is not a usable default for another's.
        """
        if key := _secret(self.LLM_DOCUMENT_API_KEY):
            return key
        if self.document_provider == self.LLM_PROVIDER:
            return self.LLM_API_KEY.get_secret_value()
        raise ValueError(
            f"LLM_DOCUMENT_PROVIDER is {self.document_provider!r} but "
            f"LLM_DOCUMENT_API_KEY is not set"
        )

    @property
    def region_mailboxes(self) -> dict[str, str]:
        """Region key in registries.yaml -> the address its RFQs are forwarded to."""
        return {"uae": self.UAE_MAILBOX, "sg": self.SG_MAILBOX}

    @property
    def region_cc(self) -> dict[str, list[str]]:
        """Region key in registries.yaml -> who is copied on its forwards."""
        return {"uae": _addresses(self.UAE_CC), "sg": _addresses(self.SG_CC)}


def _secret(value: SecretStr | None) -> str:
    """A secret's text, with unset and blank meaning the same thing."""
    return value.get_secret_value().strip() if value else ""


def _addresses(value: str) -> list[str]:
    """Split a comma-separated setting, tolerating spaces and a trailing comma.

    The field stays a plain string because pydantic-settings parses a `list[str]`
    as JSON, which "a@x.com,b@y.com" is not.
    """
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # ty: ignore
