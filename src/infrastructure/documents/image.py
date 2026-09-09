"""Images, prepared for a vision model.

Three things happen to every image, and each of them is a bug if skipped.
A photo from a phone is stored landscape with an EXIF rotation flag, so a
reader that ignores the flag shows the model a sideways drawing. A 12 MP photo
of a printed list carries no more readable text than a 1568 px one but costs
several times the tokens. And HEIC, which every recent iPhone produces, is not
a format most vision endpoints accept.
"""

import io
import logging

from PIL import Image, ImageOps

from src.infrastructure.documents.budget import Budget
from src.infrastructure.documents.models import UNREADABLE, Document, ImageRef

logger = logging.getLogger(__name__)

# JPEG for everything: quality 85 is visually lossless for text and a fraction
# of PNG's size on photographs.
_JPEG_QUALITY = 85
_MEDIA_TYPE = "image/jpeg"


def read_image(document: Document, data: bytes, budget: Budget) -> Document:
    try:
        with Image.open(io.BytesIO(data)) as opened:
            document.images.append(
                encode_image(opened, origin=document.origin, budget=budget)
            )
    except Exception as error:  # noqa: BLE001 - Pillow raises per-format types
        document.warn(UNREADABLE)
        logger.debug("Pillow could not read %s: %s", document.filename, error)

    return document


def encode_image(image: Image.Image, *, origin: str, budget: Budget) -> ImageRef:
    """Rotate by EXIF, shrink to the long-edge cap, re-encode as JPEG."""
    upright = ImageOps.exif_transpose(image) or image
    if upright.mode not in ("RGB", "L"):
        # RGBA and P both fail to save as JPEG; a paletted scan flattens fine.
        upright = upright.convert("RGB")

    edge = budget.max_image_edge
    if max(upright.size) > edge:
        upright.thumbnail((edge, edge), Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    upright.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)

    return ImageRef(
        origin=origin,
        media_type=_MEDIA_TYPE,
        data=buffer.getvalue(),
        width=upright.width,
        height=upright.height,
    )
