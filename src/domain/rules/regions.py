"""L2: which regional desk an RFQ belongs to.

The region decides who receives the forwarded email, so it is resolved by rules
over a table an operator edits, never by the model - a real address behind a
probabilistic answer cannot be audited. Two regions matching at once counts as
no match: an unrouted RFQ gets labelled for a person, a misrouted one is lost.
"""

import re
from typing import NamedTuple

from pydantic import BaseModel, Field, field_validator


def _lowercase(values: list[str]) -> list[str]:
    return [value.strip().lower() for value in values if value.strip()]


class Region(BaseModel):
    """How to recognise one regional desk, and where its RFQs go.

    `forward_to` and `cc` are filled from the environment at load, never from
    the YAML: they are real mailboxes and the YAML is committed.
    """

    forward_to: str = ""
    cc: list[str] = Field(default_factory=list)
    # The name this desk goes by in the workbook's own branch dropdown, which
    # is what decides the port list. Spelled exactly as `$W$2:$W$7` has it, or
    # left empty when the desk has no branch in the template yet.
    template_branch: str = ""
    # What this desk quotes in when the customer names no currency. One of the
    # four the workbook offers, or empty to leave the cell blank instead.
    ports: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    mailbox_markers: list[str] = Field(default_factory=list)

    _normalise = field_validator("ports", "keywords", "mailbox_markers")(_lowercase)


class RegionMatch(NamedTuple):
    """The desk an email belongs to, and which rule put it there."""

    key: str
    region: Region
    rule: str


def resolve_region(
    regions: dict[str, Region],
    *,
    region_hint: str | None = None,
    delivery_port: str | None = None,
    text: str = "",
    mailbox: str | None = None,
) -> RegionMatch | None:
    """First rule that names exactly one region wins; None means do not forward.

    Rules run from most to least trustworthy. A rule matching two regions ends
    the search rather than falling through - a genuine conflict must not be
    settled by a weaker signal.
    """
    rules = {
        "region_hint": _named(regions, region_hint),
        "delivery_port": _by_port(regions, delivery_port),
        "keyword": _by_keyword(regions, text),
        "mailbox": _by_mailbox(regions, mailbox),
    }

    for rule, keys in rules.items():
        if len(keys) > 1:
            return None
        if len(keys) == 1:
            (key,) = keys
            return RegionMatch(key=key, region=regions[key], rule=rule)
    return None


def _named(regions: dict[str, Region], hint: str | None) -> set[str]:
    """An explicit hint names the region directly, e.g. `region_hint: "UAE"`."""
    key = (hint or "").strip().lower()
    return {key} & set(regions)


def _by_port(regions: dict[str, Region], port: str | None) -> set[str]:
    """Substring match: the extracted port reads "Fujairah" or "UAE - Dubai"."""
    value = (port or "").lower()
    return {key for key, region in regions.items() if any(p in value for p in region.ports)}


def _by_keyword(regions: dict[str, Region], text: str) -> set[str]:
    """Whole-word match, so "sg" cannot fire inside "message"."""
    lowered = text.lower()
    return {
        key
        for key, region in regions.items()
        if any(_whole_word(word, lowered) for word in region.keywords)
    }


def _by_mailbox(regions: dict[str, Region], mailbox: str | None) -> set[str]:
    """The last resort: a regional mailbox is that region unless the text says otherwise."""
    tokens = set(re.split(r"[^a-z0-9]+", (mailbox or "").lower()))
    return {key for key, region in regions.items() if tokens & set(region.mailbox_markers)}


def _whole_word(word: str, text: str) -> bool:
    """`\\b` will not do: it fails on markers ending in punctuation, like "u.a.e."."""
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None
