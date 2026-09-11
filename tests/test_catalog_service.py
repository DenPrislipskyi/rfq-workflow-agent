"""Keeping the catalogue current, and what happens when Google will not answer.

The happy path is one line. What is worth a test is the promise the service
makes around it: an email that arrives while the sheet is unreachable is still
matched against the last good copy, and a sheet that comes back wrong never
replaces one that works.
"""

import pytest

from src.infrastructure.catalog import Sheet, SheetUnavailable, Snapshot, parse_csv
from src.services.catalog import CatalogService

CSV = """Item Code,Item Description,UOM
T69128400,"HEX HEAD BOLT/NUT STEEL UNGALV, M16 X 65MM",SET
T85116300,WELDER GLOVES FIVE FINGERS,PAIR
"""

GROWN = CSV + "T65082300,RULE CONVEX STEEL METRIC 5MTR,PCS\n"


class FakeSheet(Sheet):
    """Hands over whatever it was given, or raises whatever it was given."""

    def __init__(self, *answers: str | Exception) -> None:
        self.answers = list(answers)
        self.calls = 0

    async def fetch(self) -> str:
        self.calls += 1
        answer = self.answers[min(self.calls - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


def service(tmp_path, sheet: Sheet | None = None, **kwargs) -> CatalogService:
    return CatalogService(
        sheet,
        Snapshot(tmp_path / "catalog" / "items.csv"),
        code_column="Item Code",
        description_column="Item Description",
        refresh_minutes=10,
        **kwargs,
    )


# --- the snapshot ---------------------------------------------------------


async def test_the_sheet_becomes_a_snapshot_and_a_catalogue(tmp_path):
    keeper = service(tmp_path, FakeSheet(CSV))

    catalog = await keeper.refresh()

    assert len(catalog) == 2
    assert (tmp_path / "catalog" / "items.csv").read_text(encoding="utf-8") == CSV
    assert catalog.by_code("T69128400") is not None


async def test_a_restart_reads_the_snapshot_without_touching_google(tmp_path):
    """The first email of the day must not wait on a network call."""
    await service(tmp_path, FakeSheet(CSV)).refresh()

    offline = service(tmp_path, FakeSheet(SheetUnavailable("Google is down")))
    catalog = offline.load()

    assert len(catalog) == 2
    assert offline.status()["items"] == 2


async def test_no_snapshot_is_an_empty_catalogue_rather_than_a_crash(tmp_path):
    keeper = service(tmp_path)

    catalog = keeper.load()

    assert len(catalog) == 0
    assert catalog.search("anything") == []


async def test_the_snapshot_says_when_it_was_taken_and_what_is_in_it(tmp_path):
    keeper = service(tmp_path, FakeSheet(CSV))
    await keeper.refresh()

    status = keeper.status()

    assert status["items"] == 2
    assert status["fetched_at"] and status["age"]


async def test_every_column_of_the_sheet_survives_the_round_trip(tmp_path):
    """Two columns are matched on; the rest are the next feature's, and must
    not be thrown away on the way through."""
    keeper = service(tmp_path, FakeSheet(CSV))

    catalog = await keeper.refresh()

    assert catalog.by_code("T85116300").fields["UOM"] == "PAIR"


# --- when the sheet will not answer ---------------------------------------


async def test_a_failed_fetch_leaves_the_working_catalogue_alone(tmp_path):
    keeper = service(tmp_path, FakeSheet(CSV, SheetUnavailable("not shared")))
    await keeper.refresh()

    with pytest.raises(SheetUnavailable):
        await keeper.refresh()

    assert len(keeper.current) == 2


async def test_a_sheet_with_no_usable_rows_never_replaces_one_that_works(tmp_path):
    """Somebody clearing the sheet to paste a new list must not empty the
    agent's catalogue for the minute that takes."""
    keeper = service(tmp_path, FakeSheet(CSV, "Item Code,Item Description\n"))
    await keeper.refresh()

    with pytest.raises(SheetUnavailable):
        await keeper.refresh()

    assert len(keeper.current) == 2
    # And the good snapshot is still the one on disk, for the next restart.
    assert len(parse_csv((tmp_path / "catalog" / "items.csv").read_text())) == 2


async def test_no_sheet_configured_is_a_reason_not_a_failure(tmp_path):
    keeper = service(tmp_path)

    with pytest.raises(SheetUnavailable, match="CATALOG_SHEET_ID"):
        await keeper.refresh()

    await keeper.start()  # does nothing, says why, and does not raise
    await keeper.stop()


async def test_a_newer_sheet_replaces_the_catalogue_in_place(tmp_path):
    keeper = service(tmp_path, FakeSheet(CSV, GROWN))
    await keeper.refresh()

    await keeper.refresh()

    assert len(keeper.current) == 3
    assert keeper.current.by_code("T65082300") is not None
