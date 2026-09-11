"""Restate every customer wording in the sheet, and keep the answers.

    uv run python -m src.tools.describe_lines

Written once and read many times: `catalog_recall` scores these against the raw
wordings on the same cases, and that comparison is what decides whether step A
earns its model call. Without the cache the comparison would cost a call per
line every time somebody wanted to see the number.

The answers land in a file beside the snapshot, keyed by the wording they came
from, so a sheet that has grown only costs calls for what is new in it.
"""

import asyncio
import json
import logging
import sys

from src.core.config import get_settings
from src.core.lifespan import build_llm_registry
from src.core.logging import configure_logging
from src.infrastructure.catalog import Snapshot
from src.services.matching import LineDescriber
from src.tools.catalog_recall import cache_path, cases_of, read_cache

logger = logging.getLogger(__name__)


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_LIBRARY_LEVEL)

    rows = Snapshot(settings.CATALOG_SNAPSHOT_PATH).rows()
    if not rows:
        logger.error("No catalogue - run `python -m src.tools.sync_catalog` first")
        return 1

    wordings = [case.wording for case in cases_of(rows, settings)]
    if not wordings:
        logger.error("No customer wordings in the sheet to restate")
        return 1

    path = cache_path(settings)
    known = read_cache(settings)
    todo = [wording for wording in wordings if wording not in known]

    logger.info("%d wording(s), %d already restated, %d to do", len(wordings), len(known), len(todo))
    if not todo:
        return 0

    describer = LineDescriber(build_llm_registry(settings).text)
    restated = await describer.run(todo)

    known.update(dict(zip(todo, restated, strict=True)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(known, indent=2, ensure_ascii=False), encoding="utf-8")

    unchanged = sum(1 for original, text in zip(todo, restated, strict=True) if original == text)
    logger.info("Wrote %d restatement(s) to %s", len(known), path)
    logger.info("%d of the new ones came back unchanged", unchanged)
    for original, text in list(zip(todo, restated, strict=True))[:8]:
        logger.info("  %-44s -> %s", original[:44], text[:50])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
