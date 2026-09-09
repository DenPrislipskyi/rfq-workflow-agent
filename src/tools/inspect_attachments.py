"""Read a folder of real attachments and print what came out of each one.

    uv run python -m src.tools.inspect_attachments tests/fixtures/attachments
    uv run python -m src.tools.inspect_attachments <folder> --show Requisition.xlsx
    uv run python -m src.tools.inspect_attachments <folder> --prompt IMG_2201.png

The point is eyes on the output. Unit tests confirm that a reader returns a
grid; only a person looking at the grid can confirm it is the right grid, and
that is the question that decides whether the extraction stage has anything to
work with. `--prompt` shows what B2 would send a model about a file, which is
the same question one step later. Costs nothing - no model is called.
"""

import argparse
import sys
from pathlib import Path

from src.infrastructure.documents import Budget, Document, DocumentLoader, SourceFile
from src.services.extraction.table import find_blocks
from src.services.extraction.text_parts import split
from src.services.extraction.prompt import build_file_messages

_PREVIEW_ROWS = 8
_PREVIEW_COLS = 6
_CELL_WIDTH = 22


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="folder of attachments to read")
    parser.add_argument("--show", metavar="FILE", help="dump the grids of one file")
    parser.add_argument(
        "--prompt", metavar="FILE", help="print what B2 would send a model about one file"
    )
    args = parser.parse_args(argv)

    if not args.folder.is_dir():
        print(f"Not a folder: {args.folder}", file=sys.stderr)
        return 2

    paths = sorted(p for p in args.folder.iterdir() if p.is_file() and p.name != ".gitkeep")
    if not paths:
        print(f"No files in {args.folder}", file=sys.stderr)
        return 1

    documents = DocumentLoader(Budget()).load(
        [
            SourceFile(
                filename=path.name,
                data=path.read_bytes(),
                size_bytes=path.stat().st_size,
            )
            for path in paths
        ]
    )

    _print_summary(documents)

    for wanted, render in ((args.show, _print_grids), (args.prompt, _print_prompt)):
        if not wanted:
            continue
        matched = [document for document in documents if wanted in document.origin]
        if not matched:
            print(f"\nNo document matching {wanted!r}", file=sys.stderr)
            return 1
        for document in matched:
            render(document)

    return 0


def _print_summary(documents: list[Document]) -> None:
    header = f"{'FILE':<38} {'KIND':<10} {'SIZE':>9}  {'CONTENT':<28} WARNINGS"
    print(header)
    print("-" * len(header))

    for document in documents:
        print(
            f"{_clip(document.origin, 38):<38} "
            f"{document.kind.value:<10} "
            f"{_size(document.size_bytes):>9}  "
            f"{_content(document):<28} "
            f"{', '.join(document.warnings) or '-'}"
        )


def _print_grids(document: Document) -> None:
    print(f"\n{'=' * 78}\n{document.origin}")

    for grid in document.grids:
        print(f"\n  {grid.origin}  ({grid.height} x {grid.width})")
        for row in grid.rows[:_PREVIEW_ROWS]:
            cells = [_clip(cell or "", _CELL_WIDTH) for cell in row[:_PREVIEW_COLS]]
            print("    " + " | ".join(f"{cell:<{_CELL_WIDTH}}" for cell in cells))
        if grid.height > _PREVIEW_ROWS:
            print(f"    ... {grid.height - _PREVIEW_ROWS} more rows")

    if document.text:
        print(f"\n  text ({len(document.text)} chars):")
        for line in document.text.splitlines()[:_PREVIEW_ROWS]:
            print(f"    {_clip(line, 74)}")

    for image in document.images:
        print(f"\n  image {image.origin}  {image.width}x{image.height}  {_size(image.size_bytes)}")


def _print_prompt(document: Document) -> None:
    """What the file reader would show a model. Images are summarised, not
    dumped as base64."""
    print(f"\n{'=' * 78}\n{document.origin}\n")

    _, (_, content) = build_file_messages(
        document,
        [block for grid in document.grids for block in find_blocks(grid)],
        split(document),
    )
    if isinstance(content, str):
        print(content)
        return

    print(content[0]["text"])
    for block in content[1:]:
        encoded = block.get("base64", "")
        print(f"[{block['type']}: {block.get('mime_type')}, {_size(len(encoded))} base64]")


def _content(document: Document) -> str:
    parts = []
    if document.grids:
        parts.append(f"{len(document.grids)} grid(s)")
    if document.pages:
        parts.append(f"{len(document.pages)} page(s)")
    if document.images:
        parts.append(f"{len(document.images)} image(s)")
    if document.text:
        parts.append(f"{len(document.text)} chars")
    return ", ".join(parts) or "nothing"


def _size(count: int) -> str:
    if count >= 1024 * 1024:
        return f"{count / 1024 / 1024:.1f} MB"
    if count >= 1024:
        return f"{count / 1024:.0f} KB"
    return f"{count} B"


def _clip(text: str, width: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


if __name__ == "__main__":
    raise SystemExit(main())
