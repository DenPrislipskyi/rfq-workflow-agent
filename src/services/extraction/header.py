"""B4: the top of the form - vessel, port, dates, who is asking.

One call, always. Every RFQ has a header; only some have their items outside a
table, which is why B5 runs sometimes and this does not.

Nothing here interprets a value. "12 Oct" comes back as "12 Oct" and "Jebel Ali"
as "Jebel Ali", because the reader does not know which cell they are going into
and the step that does - B6 - needs the original to map it. A reader that
normalises has already thrown away its evidence.
"""

import logging

from src.domain.models import NormalizedEmail, Signals
from src.infrastructure.llm.client import LLM
from src.infrastructure.llm.exceptions import LLMError
from src.services.extraction.models import (
    DERIVED_FIELDS,
    EXPLAINED_A_FIELD_IT_FOUND,
    FIELD_CLAIMED_TWICE,
    FIELD_FROM_UNKNOWN_SOURCE,
    FIELD_IS_NOT_A_MODELS_TO_FILL,
    FIELD_WITHOUT_SOURCE,
    HEADER_FAILED,
    PLACEHOLDER_VALUE,
    REQUIRED_HEADER_FIELDS,
    HeaderField,
    HeaderValue,
    ReadDocument,
    RefusedField,
    RfqHeader,
)
from src.services.extraction.prompt import build_header_messages, source_labels
from src.services.extraction.schemas import ExtractedField, FoundValue, HeaderExtraction

logger = logging.getLogger(__name__)

# What the model must not answer with. It is told to leave a field out instead,
# but a placeholder is the reflex these strings come from and it costs nothing
# to refuse them here as well.
PLACEHOLDERS = frozenset({"n/a", "na", "none", "null", "unknown", "-", "--", "tbc", "tba", "?"})

# One sentence, and the prompt asks for under 140. A model that writes a
# paragraph is explaining rather than answering, and the desk reads the first
# line of it anyway.
MAX_WHY_CHARS = 200


class HeaderReader:
    """The header fields of one RFQ."""

    def __init__(self, llm: LLM) -> None:
        self._llm = llm

    async def read(
        self,
        email: NormalizedEmail,
        documents: list[ReadDocument],
        signals: Signals | None = None,
    ) -> RfqHeader:
        """Ask once, then keep only what survives being checked."""
        try:
            answer = await self._llm.invoke(
                build_header_messages(email, documents, signals), HeaderExtraction
            )
        except LLMError as error:
            logger.warning("Header extraction failed: %s", error)
            return RfqHeader(warnings=[HEADER_FAILED])

        header = _accept(answer.value, source_labels(documents))
        # What the model answered, and nothing about what survives mapping -
        # that is the next stage's line, and it used to print the same list of
        # missing fields three times over.
        logger.info(
            "Header: %d field(s) from %d source(s), customer %r, %d refused, %d explained",
            len(header.fields),
            len(documents) + 1,
            header.customer_company.value if header.customer_company else None,
            len(answer.value.fields) - len(header.fields),
            len(header.not_found),
        )
        for name, why in header.not_found.items():
            logger.info("Not found: %s - %s", name.value, why)
        return header


def _accept(extraction: HeaderExtraction, known_sources: set[str]) -> RfqHeader:
    """Keep the fields that hold up; say why about the ones that do not."""
    fields: dict[HeaderField, HeaderValue] = {}
    refused: list[RefusedField] = []
    warnings: list[str] = []

    for found in extraction.fields:
        problem = _problem(found, fields)
        if problem:
            logger.info("Refused %s=%r: %s", found.field.value, found.value, problem)
            refused.append(
                RefusedField(field=found.field, value=found.value.strip(), why=problem)
            )
            _warn(warnings, problem)
            continue
        if _is_unknown_source(found, known_sources):
            # Kept, because the value is probably right and the citation is only
            # worded oddly. Flagged, because it might not be.
            _warn(warnings, FIELD_FROM_UNKNOWN_SOURCE)

        fields[found.field] = HeaderValue(value=found.value.strip(), source=found.source.strip())

    company, problem = _company(extraction.customer_company)
    if problem:
        logger.info("Refused the customer company: %s", problem)
        _warn(warnings, problem)

    return RfqHeader(
        fields=fields,
        customer_company=company,
        refused=refused,
        not_found=_explanations(extraction, fields, warnings),
        warnings=warnings,
    )


def _explanations(
    extraction: HeaderExtraction,
    fields: dict[HeaderField, HeaderValue],
    warnings: list[str],
) -> dict[HeaderField, str]:
    """The model's account of each starred field it could not find.

    Held to the same standard as a value. An explanation for a field the same
    answer filled in is a contradiction - the model saying both "here it is"
    and "it is not there" - and the value is the half worth keeping. An
    explanation for a field nobody starred is noise: a missing phone number
    needs no paragraph.
    """
    kept: dict[HeaderField, str] = {}

    for missing in extraction.not_found:
        why = " ".join(missing.why.split())[:MAX_WHY_CHARS]
        if not why:
            continue
        if missing.field in fields:
            logger.info(
                "Dropped the note on %s: it was filled in as %r",
                missing.field.value,
                fields[missing.field].value,
            )
            _warn(warnings, EXPLAINED_A_FIELD_IT_FOUND)
            continue
        if missing.field not in REQUIRED_HEADER_FIELDS:
            continue
        kept[missing.field] = why

    return kept


def _company(found: FoundValue | None) -> tuple[HeaderValue | None, str]:
    """The company that sent the RFQ, held to the same standard as a field.

    It is not a cell, but it decides one - the sender code - so a placeholder
    or an uncited value is no more welcome here than anywhere else.
    """
    if found is None:
        return None, ""
    value = found.value.strip()
    if not value or value.lower() in PLACEHOLDERS:
        return None, PLACEHOLDER_VALUE
    if not found.source.strip():
        return None, FIELD_WITHOUT_SOURCE
    return HeaderValue(value=value, source=found.source.strip()), ""


def _problem(found: ExtractedField, taken: dict[HeaderField, HeaderValue]) -> str:
    """The reasons a field does not get in. Empty string means it does."""
    if found.field in DERIVED_FIELDS:
        # The sender code is looked up, never read. A model that answers with
        # one has produced a plausible code belonging to somebody else.
        return FIELD_IS_NOT_A_MODELS_TO_FILL
    if found.field in taken:
        # The first answer stands. A second is the model changing its mind
        # mid-response, and there is nothing to prefer about the later guess.
        return FIELD_CLAIMED_TWICE

    value = found.value.strip()
    if not value or value.lower() in PLACEHOLDERS:
        # "N/A" is not a value. It is a way of not leaving the field out.
        return PLACEHOLDER_VALUE
    if not found.source.strip():
        # A value that cannot say where it came from was invented.
        return FIELD_WITHOUT_SOURCE
    return ""


def _is_unknown_source(found: ExtractedField, known_sources: set[str]) -> bool:
    """The citation has to point at something we actually showed the model."""
    cited = found.source.strip()
    return not any(label in cited or cited in label for label in known_sources)


def _warn(warnings: list[str], code: str) -> None:
    if code not in warnings:
        warnings.append(code)
