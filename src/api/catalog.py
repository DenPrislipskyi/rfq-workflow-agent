"""The product sheet, for a person choosing from it by hand.

The matching screen offers five candidates per line. When none of them is the
product, somebody has to go and find it, and this is what they search. Two
fields, because that is how the desk actually looks: by our code when they know
it, by words when they do not.

Read-only, and it never leaves the copy already in memory. The sheet is
re-fetched on its own schedule; a page asking for products must not become a
reason to call Google.
"""

import logging
from collections.abc import Iterable

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from src.api.dependencies import CatalogDep
from src.domain.rules.catalog import CatalogItem, normalize_code

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/catalog", tags=["Catalog"])

# What one page of the picker holds. High enough that a search worth making
# fits in it, low enough that "show me everything" on a sheet of fifty thousand
# rows is not a megabyte of JSON nobody scrolls.
PAGE = 50
MOST = 200


class Wire(BaseModel):
    """A model that goes out camelCased, because a TypeScript client reads it."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Product(Wire):
    """One row of the sheet, as the picker shows it."""

    item_code: str
    description: str
    # Every column, the same as a candidate carries. The picker shows the same
    # `Source` and `UOM` as the table does, and it reads them from here.
    item: dict[str, str] = Field(default_factory=dict)


class Products(Wire):
    """One page of the sheet."""

    items: list[Product] = Field(default_factory=list)
    # How many rows matched, not how many came back. A picker that says "50
    # results" when there are four hundred teaches people to stop typing.
    total: int = 0


def _matches(item: CatalogItem, code: str, words: str) -> bool:
    """Whether one row answers both filters.

    Substring rather than the search index on purpose: this is a person
    scanning a sheet, not a line of an RFQ being matched. They type half a code
    or half a word and expect to see it, and BM25 would rank `M16` above the
    `M16` they were actually looking for while dropping `M1` entirely.
    """
    return (code in item.code.upper()) and (words in item.description.upper())


def _one_each(items: Iterable[CatalogItem]) -> list[CatalogItem]:
    """One entry per product, keeping the first row that carries it.

    A row of the sheet is a **case of a mapping**, not a product: `T31237400`
    sits in it twice, once for the 3XL boilersuit a customer asked for and once
    for the 2XL. Offered as rows, the same product appears in the picker two or
    three times, and somebody choosing between two identical lines is choosing
    between nothing.

    The shortlist already collapses this way (`_best_per_product`), and the
    picker offers the same products; the two would disagree otherwise.
    """
    seen: dict[str, CatalogItem] = {}
    for item in items:
        seen.setdefault(normalize_code(item.code), item)
    return list(seen.values())


@router.get("/items", status_code=status.HTTP_200_OK)
async def read_products(
    catalog: CatalogDep,
    code: str = Query("", description="Part of our own item code"),
    description: str = Query("", description="Part of our own product description"),
    limit: int = Query(PAGE, ge=1, le=MOST),
) -> Products:
    """The sheet's products, narrowed by either field or by neither.

    Both filters empty is not an error - it is the picker being opened before
    anybody types, and it answers with the first page of the sheet.
    """
    wanted = _one_each(
        item
        for item in catalog.current.items
        if _matches(item, code.strip().upper(), description.strip().upper())
    )

    return Products(
        items=[
            Product(item_code=item.code, description=item.description, item=dict(item.fields))
            for item in wanted[:limit]
        ],
        total=len(wanted),
    )
