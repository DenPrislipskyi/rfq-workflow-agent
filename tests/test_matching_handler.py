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
from src.services.matching import AgreementJudge, MatchingPipeline
from src.services.matching.schemas import Judgement, Judgements
from tests.fakes import BrokenLLM
from tests.test_extraction_handler import Router, attachment, build_handler, message

# The rope is the first line of the requisition every test reads back, and this
# row is what its code leads to. What is judged is that line against this row's
# `Item Description / SSG Description`.
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

    Kept as a parameter although matching no longer reads that column: it is
    still what `Catalog` carries, and a test that sets it proves the column is
    not quietly back in the question.
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

    def __init__(self, same: bool = True) -> None:
        super().__init__()
        self._same = same

    async def invoke(self, messages, schema):
        if schema.__name__ == "Judgements":
            self.asked.append(schema.__name__)
            return LLMResult(
                value=Judgements(
                    items=[Judgement(index=0, same=self._same, why="the judge said so")]
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
            AgreementJudge(matching_llm or llm),
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


async def test_a_code_that_names_what_was_asked_for_is_confirmed(tmp_path: Path):
    """The first branch: the code names a product, and it is what the line
    asked for. One product, no shortlist - nothing to choose between."""
    handler, _, _ = build(tmp_path)

    await handler.handle(message(attachment()))

    line = recorded(tmp_path)["matching"][0]
    assert line["customer_code"] == "550101"
    assert line["item_code"] == "T55010100"
    assert line["how"] == "code_confirmed"
    # Five of the seven words of `ROPE PP 24MM X 220M` are in `ROPE
    # POLYPROPYLENE 24MM X 220MTR`. Scored like a candidate, from the same two
    # sentences the judge read.
    assert line["confidence"] == 71
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


async def test_a_code_that_names_something_else_is_dropped(tmp_path: Path):
    """The second branch, and the desk's own rule: a code that does not name
    what the line asked for loses to the words."""
    handler, _, _ = build(tmp_path, llm=MatchingRouter(same=False))

    await handler.handle(message(attachment()))

    line = recorded(tmp_path)["matching"][0]
    assert line["how"] == "code_rejected"
    assert line["item_code"] is None, "the rejected code is not the answer"
    assert line["item"] == {}
    assert "not what this line asked for" in line["why"]
    assert "the judge said so" in line["why"]


async def test_a_candidate_carries_the_whole_row_too(tmp_path: Path):
    """A shortlisted product fills the same columns of the table as a confirmed
    one. A row with nothing in it is not a candidate anybody can judge."""
    handler, _, _ = build(tmp_path, llm=MatchingRouter(same=False))

    await handler.handle(message(attachment()))

    line = recorded(tmp_path)["matching"][0]
    assert [one["item_code"] for one in line["candidates"]] == ["T55010100"]
    assert line["candidates"][0]["item"] == ROW
    # Five of the seven words of "ROPE PP 24MM X 220M" are in "ROPE
    # POLYPROPYLENE 24MM X 220MTR": rope, 24mm, 24, x and 220 - the last
    # because a number welded to its unit also counts as the bare number, so
    # `220M` and `220MTR` are not two different lengths. `pp` is not there.
    assert line["candidates"][0]["confidence"] == 71


async def test_an_overruled_line_is_searched_by_its_own_words(tmp_path: Path):
    """And the customer's own quantity and unit survive, unconverted: the sheet
    has units of its own and they are not these."""
    handler, _, _ = build(
        tmp_path, llm=MatchingRouter(same=False), shelf_wording="ROPE NYLON 24MM"
    )

    await handler.handle(message(attachment()))

    line = recorded(tmp_path)["matching"][0]
    assert line["quantity"] and line["uom"]
    assert line["verbatim"] == "ROPE PP 24MM X 220M"
    assert line["query"] == "ROPE PP 24MM X 220M", "the email's words, not the sheet's"


async def test_a_confirmed_line_was_searched_for_nothing(tmp_path: Path):
    handler, _, _ = build(tmp_path)

    await handler.handle(message(attachment()))

    assert recorded(tmp_path)["matching"][0]["query"] == ""


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
    assert asked.index("Judgements") == len(asked) - 1


async def test_matching_that_fails_costs_the_email_nothing(tmp_path: Path):
    """The judging call is the only model call left in matching, and losing it
    confirms nothing rather than confirming blindly."""
    broken = BrokenLLM(LLMCallError("fake", TimeoutError("no answer")))
    handler, box, _ = build(tmp_path, matching_llm=broken)

    await handler.handle(message(attachment()))

    assert box.forwards
    written = recorded(tmp_path)
    assert written["delivery"]["outcome"] == "SENT"
    assert written["matching"], "every line still comes back"
    assert written["matching"][0]["verbatim"] == "ROPE PP 24MM X 220M"
    assert written["matching"][0]["item_code"] is None, "an unchecked code is not trusted"


async def test_an_email_that_is_not_an_rfq_is_never_matched(tmp_path: Path):
    handler, _, _ = build(tmp_path)

    await handler.handle(message())

    assert recorded(tmp_path)["matching"] == []
