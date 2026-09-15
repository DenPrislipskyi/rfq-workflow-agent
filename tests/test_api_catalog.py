"""The product sheet, as the picker on the matching screen reads it.

The contract is narrow: two substring filters over our own code and our own
description, a page of rows, and a count of everything that matched. What it
must not do is rank, guess or reach for the sheet over the network - a person
scrolling a list is not a line of an RFQ being matched.
"""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import register_exceptions, register_routers
from src.api.dependencies import get_catalog
from src.domain.rules.catalog import Catalog

URL = "/api/v1/catalog/items"

ROWS = [
    {"Item Code": "T69128400", "Item Description": "HEX HEAD BOLT/NUT STEEL UNGALV, M16 X 65MM",
     "Product Source": "Stock", "UOM": "SET"},
    {"Item Code": "T69133100", "Item Description": "HEX HEAD BOLT/NUT STEEL UNGALV, M20 X 80MM",
     "Product Source": "JIT", "UOM": "SET"},
    {"Item Code": "T85116300", "Item Description": "WELDER GLOVES FIVE FINGERS",
     "Product Source": "Stock", "UOM": "PRS"},
    {"Item Code": "T33410300", "Item Description": "SAFETY SIGN DAVIT-LAUNCHED LIFERAFT 150X150",
     "Product Source": "GPL", "UOM": "PCS"},
]


def client(rows=None) -> TestClient:
    app = FastAPI()
    register_exceptions(app)
    register_routers(app)
    app.dependency_overrides[get_catalog] = lambda: SimpleNamespace(
        current=Catalog.from_rows(
            ROWS if rows is None else rows,
            code_column="Item Code",
            description_column="Item Description",
        )
    )
    return TestClient(app)


def test_asking_for_nothing_in_particular_answers_with_the_sheet():
    """The picker opened before anybody typed. Not an error - a first page."""
    body = client().get(URL).json()

    assert body["total"] == 4
    assert [one["itemCode"] for one in body["items"]] == [
        "T69128400",
        "T69133100",
        "T85116300",
        "T33410300",
    ]


def test_a_product_carries_its_whole_row():
    """The picker shows the same Source and UOM the table does, and reads them
    from the same place a candidate does."""
    item = client().get(URL, params={"code": "T85116300"}).json()["items"][0]

    assert item["description"] == "WELDER GLOVES FIVE FINGERS"
    assert item["item"]["Product Source"] == "Stock"
    assert item["item"]["UOM"] == "PRS"


def test_the_code_filter_matches_part_of_a_code():
    body = client().get(URL, params={"code": "6913"}).json()

    assert [one["itemCode"] for one in body["items"]] == ["T69133100"]
    assert body["total"] == 1


def test_the_description_filter_matches_part_of_a_description():
    body = client().get(URL, params={"description": "gloves"}).json()

    assert [one["itemCode"] for one in body["items"]] == ["T85116300"]


def test_both_filters_narrow_together():
    """Two fields because that is how the desk looks: by code when they know
    it, by words when they do not, and by both when the code is half-remembered."""
    body = client().get(URL, params={"code": "T691", "description": "M20"}).json()

    assert [one["itemCode"] for one in body["items"]] == ["T69133100"]


def test_neither_filter_cares_about_case_or_stray_spaces():
    body = client().get(URL, params={"code": " t691284 ", "description": " hex head "}).json()

    assert [one["itemCode"] for one in body["items"]] == ["T69128400"]


def test_nothing_matching_is_an_empty_page_not_an_error():
    body = client().get(URL, params={"description": "turbocharger"}).json()

    assert (body["items"], body["total"]) == ([], 0)


def test_the_count_is_of_everything_that_matched_not_of_what_came_back():
    """A picker that says "50 results" when there are four hundred teaches
    people to stop typing."""
    body = client().get(URL, params={"description": "BOLT", "limit": 1}).json()

    assert len(body["items"]) == 1
    assert body["total"] == 2


# --- one entry per product, not per row ------------------------------------

TWICE = [
    {"Item Code": "T31237400", "Item Description": "BOILERSUIT COTTON NAVY",
     "Customer Description": "Boilersuit 3XL"},
    {"Item Code": "T31237400", "Item Description": "BOILERSUIT COTTON NAVY",
     "Customer Description": "Boilersuit 2XL"},
    {"Item Code": "T31237100", "Item Description": "BOILERSUIT COTTON NAVY L"},
]


def test_a_product_the_sheet_carries_twice_is_offered_once():
    """A row of the sheet is a case of a mapping, not a product. Offered as
    rows, somebody choosing between two identical lines chooses nothing."""
    body = client(TWICE).get(URL, params={"description": "boilersuit"}).json()

    assert [one["itemCode"] for one in body["items"]] == ["T31237400", "T31237100"]


def test_the_count_is_of_products_too():
    assert client(TWICE).get(URL, params={"description": "boilersuit"}).json()["total"] == 2


def test_a_repeated_code_keeps_the_first_row_it_came_from():
    """Same rule as the shortlist: the first row wins, so the two never
    disagree about which row a product is shown from."""
    item = client(TWICE).get(URL, params={"code": "T31237400"}).json()["items"][0]

    assert item["item"]["Customer Description"] == "Boilersuit 3XL"


def test_a_code_written_two_ways_is_one_product():
    body = client(
        [
            {"Item Code": "T-690/302", "Item Description": "HEX BOLT M12"},
            {"Item Code": "T690302", "Item Description": "HEX BOLT M12"},
        ]
    ).get(URL, params={"description": "hex"}).json()

    assert body["total"] == 1


def test_a_page_cannot_be_asked_to_be_unbounded():
    assert client().get(URL, params={"limit": 5000}).status_code == 422
    assert client().get(URL, params={"limit": 0}).status_code == 422
