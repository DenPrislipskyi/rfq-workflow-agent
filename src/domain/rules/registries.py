"""Domain knowledge an operator can edit without a release.

Loaded and validated once at startup, never per request: a malformed YAML must
break the boot, not the first email of the day.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from src.domain.enums import RecommendedAction, SenderClass
from src.domain.models import ClassificationResult


def _lowercase(values: list[str]) -> list[str]:
    return [value.strip().lower() for value in values if value.strip()]


class Portal(BaseModel):
    """A procurement portal that emails the chandler on a customer's behalf."""

    domains: list[str] = Field(default_factory=list)
    body_markers: list[str] = Field(default_factory=list)
    subject_patterns: dict[str, list[str]] = Field(default_factory=dict)
    # Does an RFQ from here reach SCINT on its own? Unproven so far, so the
    # default is false and nothing is silently ignored.
    is_integrated: bool = False

    _normalise_domains = field_validator("domains")(_lowercase)


class FastPathConfidence(BaseModel):
    internal_only: float
    empty_body: float


class FastPathSettings(BaseModel):
    min_body_chars: int
    confidence: FastPathConfidence


class OutlookCategories(BaseModel):
    """Which Outlook labels a decision earns.

    Every action maps to something on purpose, because "no label" is what tells
    an operator the email never reached the agent at all.
    """

    by_action: dict[RecommendedAction, str] = Field(default_factory=dict)
    needs_review: str | None = None
    error: str | None = None

    def for_result(self, result: ClassificationResult) -> list[str]:
        """Labels for one decision: where it goes, plus a flag if a human is needed."""
        names = []
        routed = self.by_action.get(result.recommended_action)
        if routed:
            names.append(routed)
        if result.needs_human_review and self.needs_review not in (None, *names):
            names.append(self.needs_review)
        return names

    def for_failure(self) -> list[str]:
        return [self.error] if self.error else []


class Registries(BaseModel):
    """The editable registries, as loaded from YAML.

    Every list is empty by default and was measured to change nothing on the
    current corpus; they stay so that putting one back is a YAML edit rather
    than a code change.
    """

    # Filled from MAILBOX_ADDRESS at startup: the mailbox we watch is ours.
    internal_domains: list[str] = Field(default_factory=list)
    internal_system_senders: list[str] = Field(default_factory=list)
    portals: dict[str, Portal] = Field(default_factory=dict)
    known_customer_domains: list[str] = Field(default_factory=list)
    known_supplier_domains: list[str] = Field(default_factory=list)
    template_markers: dict[str, list[str]] = Field(default_factory=dict)
    outlook_categories: OutlookCategories = Field(default_factory=OutlookCategories)
    fast_path: FastPathSettings

    _normalise = field_validator(
        "internal_domains",
        "internal_system_senders",
        "known_customer_domains",
        "known_supplier_domains",
    )(_lowercase)

    @classmethod
    def load(cls, path: Path, *, mailbox: str | None = None) -> "Registries":
        """Read the file, treating the watched mailbox's domain as our own.

        Deriving it beats a second copy in YAML that has to be kept in step.
        """
        registries = cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        domain = mailbox.rsplit("@", 1)[-1].lower() if mailbox and "@" in mailbox else None
        if domain and domain not in registries.internal_domains:
            registries.internal_domains.append(domain)
        return registries

    def is_internal_domain(self, domain: str | None) -> bool:
        return bool(domain) and domain.lower() in self.internal_domains

    def is_system_sender(self, address: str | None) -> bool:
        return bool(address) and address.lower() in self.internal_system_senders

    def classify_sender(self, address: str | None, domain: str | None) -> SenderClass:
        """Place the sender against the registries. Order matters: most specific first."""
        if not domain:
            return SenderClass.UNKNOWN
        if self.is_system_sender(address):
            return SenderClass.INTERNAL_SYSTEM
        if self.is_internal_domain(domain):
            return SenderClass.INTERNAL_OWN
        if self.find_portal_by_domain(domain):
            return SenderClass.PORTAL
        if domain.lower() in self.known_customer_domains:
            return SenderClass.KNOWN_CUSTOMER
        if domain.lower() in self.known_supplier_domains:
            return SenderClass.KNOWN_SUPPLIER
        return SenderClass.EXTERNAL_UNKNOWN

    def find_portal_by_domain(self, domain: str | None) -> str | None:
        if not domain:
            return None
        for name, portal in self.portals.items():
            if domain.lower() in portal.domains:
                return name
        return None

    def find_portal_in_text(self, text: str) -> str | None:
        """Portals that mail from arbitrary addresses are recognised by their body."""
        lowered = text.lower()
        for name, portal in self.portals.items():
            if any(marker.lower() in lowered for marker in portal.body_markers):
                return name
        return None

    def matching_markers(self, text: str) -> list[str]:
        """Names of the template groups whose markers appear in this text."""
        lowered = text.lower()
        return [
            group
            for group, markers in self.template_markers.items()
            if any(marker.lower() in lowered for marker in markers)
        ]

