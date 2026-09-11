"""Matching, where it meets a real email.

Two promises are under test, and neither is "it matches well". The first is
that the desk gets its RFQ whatever matching does - it runs after the forward
and can neither delay nor fail it. The second is that what reaches the record
is the whole row of the product sheet, because that record is what somebody
reads a week later when a match looks wrong.

Driven through `handler.handle` rather than through the pieces, because the
order of the steps is half of what is being promised.
"""

import json
from pathlib import Path

from src.domain.rules.catalog import Catalog
from src.infrastructure.llm.client import LLMResult
from src.infrastructure.llm.exceptions import LLMCallError
from src.infrastructure.storage.records import EmailRecords
from src.services.matching import ItemChooser, LineDescriber, MatchingPipeline
from src.services.matching.schemas import ChosenItem, ChosenItems, RestatedLine, RestatedLines
from tests.fakes import BrokenLLM
from tests.test_extraction_handler import Router, attachment, build_handler, message

ROW = {
    "Sr #": "6",
    "Customer Code": "691284",
    "Item Code": "T69128400",
    "Customer Description": "Hexagon Head Bolts Full Threaded (Bolt with Nut) M16*65",
    "Item Description / SSG Description": "HEX HEAD BOLT/NUT STEEL UNGALV, M16 X 65MM",
    "UOM": "SET",
    "Price": "0.42",
    "Branch": "Seven Seas Shipchandlers (LLC) (Dubai)",
}


def catalog() -> Catalog:
    return Catalog.from_rows(
        [ROW],
        code_column="Item Code",
        description_column="Item Description / SSG Description",
        customer_code_column="Customer Code",
    )


class MatchingRouter(Router):
    """The handler's own double, plus answers for the two matching calls."""

    def __init__(self, item_code: str | None = "T69128400", why: str = "Same bolt.") -> None:
        super().__init__()
        self._item_code = item_code
        self._why = why

    async def invoke(self, messages, schema):
        if schema.__name__ == "RestatedLines":
            self.asked.append(schema.__name__)
            return LLMResult(
                value=RestatedLines(
                    lines=[RestatedLine(index=0, description="HEX HEAD BOLT NUT M16 X 65MM")]
                ),
                model="fake",
                latency_ms=1,
            )
        if schema.__name__ == "ChosenItems":
            self.asked.append(schema.__name__)
            return LLMResult(
                value=ChosenItems(
                    lines=[ChosenItem(index=0, item_code=self._item_code, why=self._why)]
                ),
                model="fake",
                latency_ms=1,
            )
        return await super().invoke(messages, schema)


def build(tmp_path: Path, llm=None, matching_llm=None):
    """The handler of `test_extraction_handler`, with a catalogue behind it."""
    llm = llm or MatchingRouter()
    records = EmailRecords(tmp_path / "Database", enabled=True)
    handler, box, _ = build_handler(
        tmp_path,
        llm=llm,
        records=records,
        matching=MatchingPipeline(
            LineDescriber(matching_llm or llm),
            ItemChooser(matching_llm or llm),
            candidates=5,
        ),
        catalog=catalog,
    )
    return handler, box, records


def recorded(tmp_path: Path) -> dict:
    """The one record this email produced, read off disk."""
    folders = [path for path in (tmp_path / "Database").iterdir() if path.is_dir()]
    assert len(folders) == 1
    return json.loads((folders[0] / "email.json").read_text(encoding="utf-8"))


async def test_the_whole_row_of_the_sheet_reaches_the_record(tmp_path: Path):
    """Not the two columns matching uses - all of them. Which ones will matter
    a week from now is not a decision to make today."""
    handler, _, _ = build(tmp_path)

    await handler.handle(message(attachment()))

    line = recorded(tmp_path)["matching"][0]
    assert line["item_code"] == "T69128400"
    # This requisition quotes no codes of its own, so the words are what found
    # it. Which route produced which answer is covered in `test_matching`.
    assert line["how"] == "search"
    assert line["item"] == ROW
    assert line["item"]["Price"] == "0.42"
    assert line["item"]["Branch"].startswith("Seven Seas")


async def test_the_line_keeps_both_what_was_written_and_what_we_made_of_it(tmp_path: Path):
    """And the customer's own quantity and unit, unconverted: the sheet has
    units of its own and they are not these."""
    handler, _, _ = build(tmp_path)

    await handler.handle(message(attachment()))

    line = recorded(tmp_path)["matching"][0]
    assert line["quantity"] and line["uom"]
    assert line["description"] == "HEX HEAD BOLT NUT M16 X 65MM"
    assert line["verbatim"] and line["verbatim"] != line["description"]
    assert [one["item_code"] for one in line["candidates"]] == ["T69128400"]


async def test_a_refusal_carries_no_product_and_keeps_its_reason(tmp_path: Path):
    handler, _, _ = build(tmp_path, llm=MatchingRouter(None, "Nothing here is that bolt."))

    await handler.handle(message(attachment()))

    line = recorded(tmp_path)["matching"][0]
    assert line["item_code"] is None
    assert line["item"] == {}
    assert line["how"] == "none"
    assert line["why"] == "Nothing here is that bolt."


async def test_the_rfq_is_forwarded_before_anything_is_matched(tmp_path: Path):
    """Nothing the desk receives depends on a match, so nobody waits for one."""
    handler, box, _ = build(tmp_path)

    await handler.handle(message(attachment()))

    assert box.forwards
    asked = handler._triage._pipeline._llm.asked
    assert asked.index("ChosenItems") == len(asked) - 1


async def test_matching_that_fails_costs_the_email_nothing(tmp_path: Path):
    """The newest step in the pipeline, and the only one nothing downstream
    depends on yet. It fails by itself."""
    broken = BrokenLLM(LLMCallError("fake", TimeoutError("no answer")))
    handler, box, _ = build(tmp_path, matching_llm=broken)

    await handler.handle(message(attachment()))

    assert box.forwards
    written = recorded(tmp_path)
    assert written["delivery"]["outcome"] == "SENT"
    assert written["matching"] and all(line["item_code"] is None for line in written["matching"])


async def test_an_email_that_is_not_an_rfq_is_never_matched(tmp_path: Path):
    handler, _, _ = build(tmp_path)

    await handler.handle(message())

    assert recorded(tmp_path)["matching"] == []
