"""Matching, where it meets a real email.

Two promises are under test, and neither is "it matches well". The first is
that the desk gets its RFQ whatever matching does - it runs after the forward
and can neither delay nor fail it. The second is that what reaches the record
is the whole row of the product sheet, for a confirmed product and for a
candidate alike, because that record is what somebody reads a week later when
a match looks wrong.

Driven through `handler.handle` rather than through the pieces, because the
order of the steps is half of what is being promised.
"""

import json
from pathlib import Path

from src.domain.rules.catalog import Catalog
from src.infrastructure.llm.client import LLMResult
from src.infrastructure.llm.exceptions import LLMCallError
from src.infrastructure.storage.records import EmailRecords
from src.services.matching import LineDescriber, MatchingPipeline
from src.services.matching.schemas import RestatedLine, RestatedLines
from tests.fakes import BrokenLLM
from tests.test_extraction_handler import Router, attachment, build_handler, message

# The rope is the first line of the requisition every test reads back, and this
# row is the sheet's record of a customer once asking for it. The wording in
# `Customer Description` is what the search reads.
ROW = {
    "Sr #": "6",
    "Customer Code": "550101",
    "Item Code": "T55010100",
    "Customer Description": "ROPE PP 24MM X 220M",
    "Item Description / SSG Description": "ROPE POLYPROPYLENE 24MM X 220MTR",
    "UOM": "COIL",
    "Price": "0.42",
    "Branch": "Seven Seas Shipchandlers (LLC) (Dubai)",
}


def catalog(customer_description: str | None = None) -> Catalog:
    """The one-row sheet. `customer_description` is what it files the rope under.

    A parameter because that column is now half of every comparison: a line
    disagrees with the code it quotes only when the sheet says something else
    about the product, and the requisition's own wording cannot be changed.
    """
    row = ROW if customer_description is None else ROW | {"Customer Description": customer_description}
    return Catalog.from_rows(
        [row],
        code_column="Item Code",
        description_column="Item Description / SSG Description",
        customer_code_column="Customer Code",
        customer_description_column="Customer Description",
    )


class MatchingRouter(Router):
    """The handler's own double, plus an answer for the one matching call."""

    def __init__(self, restated: str = "ROPE PP 24MM X 220M") -> None:
        super().__init__()
        self._restated = restated

    async def invoke(self, messages, schema):
        if schema.__name__ == "RestatedLines":
            self.asked.append(schema.__name__)
            return LLMResult(
                value=RestatedLines(
                    lines=[RestatedLine(index=0, description=self._restated)]
                ),
                model="fake",
                latency_ms=1,
            )
        return await super().invoke(messages, schema)


def build(tmp_path: Path, llm=None, matching_llm=None, shelf_wording: str | None = None):
    """The handler of `test_extraction_handler`, with a catalogue behind it."""
    llm = llm or MatchingRouter()
    records = EmailRecords(tmp_path / "Database", enabled=True)
    handler, box, _ = build_handler(
        tmp_path,
        llm=llm,
        records=records,
        matching=MatchingPipeline(
            LineDescriber(matching_llm or llm),
            candidates=5,
        ),
        catalog=lambda: catalog(shelf_wording),
    )
    return handler, box, records


def recorded(tmp_path: Path) -> dict:
    """The one record this email produced, read off disk."""
    folders = [path for path in (tmp_path / "Database").iterdir() if path.is_dir()]
    assert len(folders) == 1
    return json.loads((folders[0] / "email.json").read_text(encoding="utf-8"))


async def test_a_code_the_sheet_carries_is_confirmed_by_the_wording(tmp_path: Path):
    """The first branch: the customer's code names an item, and our restated
    line is already in that item's own customer wording. One product, no
    shortlist - there was nothing to choose between."""
    handler, _, _ = build(tmp_path)

    await handler.handle(message(attachment()))

    line = recorded(tmp_path)["matching"][0]
    assert line["customer_code"] == "550101"
    assert line["item_code"] == "T55010100"
    assert line["how"] == "code_confirmed"
    assert line["confidence"] == 100
    assert line["candidates"] == [], "a confirmed code produces no shortlist"


async def test_the_whole_row_of_the_sheet_reaches_the_record(tmp_path: Path):
    """Not the two columns matching uses - all of them. Which ones will matter
    a week from now is not a decision to make today."""
    handler, _, _ = build(tmp_path)

    await handler.handle(message(attachment()))

    line = recorded(tmp_path)["matching"][0]
    assert line["item"] == ROW
    assert line["item"]["Price"] == "0.42"
    assert line["item"]["Branch"].startswith("Seven Seas")


async def test_a_code_whose_wording_disagrees_is_dropped_and_searched_instead(
    tmp_path: Path,
):
    """The second branch, and the desk's own rule: the code named an item whose
    customer wording is not what was asked for, so the code loses."""
    handler, _, _ = build(
        tmp_path,
        llm=MatchingRouter("ANCHOR CHAIN STUD LINK 28MM"),
        shelf_wording="FENDER PNEUMATIC 3300 X 6500",
    )

    await handler.handle(message(attachment()))

    line = recorded(tmp_path)["matching"][0]
    assert line["how"] == "code_rejected"
    assert line["item_code"] is None, "the rejected code is not the answer"
    assert line["item"] == {}
    assert "rejected" in line["why"]


async def test_a_candidate_carries_the_whole_row_too(tmp_path: Path):
    """A shortlisted product fills the same columns of the table as a confirmed
    one. A row with nothing in it is not a candidate anybody can judge."""
    handler, _, _ = build(
        tmp_path,
        llm=MatchingRouter("ROPE NYLON BRAIDED 24MM HEAVY DUTY MARINE GRADE"),
        shelf_wording="ROPE NYLON 24MM",
    )

    await handler.handle(message(attachment()))

    line = recorded(tmp_path)["matching"][0]
    assert line["how"] == "code_rejected"
    assert [one["item_code"] for one in line["candidates"]] == ["T55010100"]
    assert line["candidates"][0]["item"] == ROW | {"Customer Description": "ROPE NYLON 24MM"}
    assert line["candidates"][0]["confidence"] == 100, "the best of its own line"


async def test_the_line_keeps_both_what_was_written_and_what_we_made_of_it(tmp_path: Path):
    """And the customer's own quantity and unit, unconverted: the sheet has
    units of its own and they are not these."""
    handler, _, _ = build(tmp_path, llm=MatchingRouter("ROPE POLYPROP 24MM 220M"))

    await handler.handle(message(attachment()))

    line = recorded(tmp_path)["matching"][0]
    assert line["quantity"] and line["uom"]
    assert line["description"] == "ROPE POLYPROP 24MM 220M"
    assert line["verbatim"] and line["verbatim"] != line["description"]


async def test_a_line_the_catalogue_cannot_answer_keeps_its_reason(tmp_path: Path):
    """The paint and the gasket are not in this one-row sheet, and no code of
    theirs is either. Nothing found, and the record says so."""
    handler, _, _ = build(tmp_path)

    await handler.handle(message(attachment()))

    line = recorded(tmp_path)["matching"][1]
    assert line["item_code"] is None
    assert line["item"] == {}
    assert line["how"] == "none"
    assert line["why"]


async def test_the_rfq_is_forwarded_before_anything_is_matched(tmp_path: Path):
    """Nothing the desk receives depends on a match, so nobody waits for one."""
    handler, box, _ = build(tmp_path)

    await handler.handle(message(attachment()))

    assert box.forwards
    asked = handler._triage._pipeline._llm.asked  # noqa: SLF001
    assert asked.index("RestatedLines") == len(asked) - 1


async def test_matching_that_fails_costs_the_email_nothing(tmp_path: Path):
    """The restating call is the only model call left in matching, and losing
    it falls back to the customer's own words rather than to no answer."""
    broken = BrokenLLM(LLMCallError("fake", TimeoutError("no answer")))
    handler, box, _ = build(tmp_path, matching_llm=broken)

    await handler.handle(message(attachment()))

    assert box.forwards
    written = recorded(tmp_path)
    assert written["delivery"]["outcome"] == "SENT"
    assert written["matching"], "every line still comes back"
    assert written["matching"][0]["verbatim"] == "ROPE PP 24MM X 220M"


async def test_an_email_that_is_not_an_rfq_is_never_matched(tmp_path: Path):
    handler, _, _ = build(tmp_path)

    await handler.handle(message())

    assert recorded(tmp_path)["matching"] == []
