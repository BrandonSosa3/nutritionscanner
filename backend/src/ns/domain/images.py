"""Image identity and validation. Pure — no I/O, no database.

Two jobs: produce the content hash that makes ingestion idempotent, and refuse
anything that is not actually an image. The uploaded content type is a claim
by the client, not evidence, so the bytes are decoded to confirm it.
"""

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

# Formats a phone camera or a screenshot can realistically produce.
ALLOWED_FORMATS: dict[str, str] = {
    "JPEG": "jpg",
    "PNG": "png",
    "HEIF": "heic",
    "WEBP": "webp",
}

MAX_IMAGE_BYTES = 25 * 1024 * 1024  # 25 MB — a 48MP phone photo fits comfortably
MIN_IMAGE_BYTES = 1024  # below this it cannot be a legible receipt
MIN_EDGE_PIXELS = 200  # a 200px-wide image cannot carry readable receipt text


class InvalidImageError(ValueError):
    """The uploaded bytes are not a usable receipt image."""


@dataclass(frozen=True, slots=True)
class ImageFacts:
    """Everything ingestion needs to know about an uploaded file."""

    sha256: str
    size_bytes: int
    width: int
    height: int
    image_format: str  # PIL format name, e.g. "JPEG"
    extension: str  # canonical extension, derived from content not filename


def sha256_hex(data: bytes) -> str:
    """Content hash. The idempotency key for the whole pipeline."""
    return hashlib.sha256(data).hexdigest()


def inspect_image(data: bytes) -> ImageFacts:
    """Validate bytes as an image and describe them.

    Raises InvalidImageError with a message suitable for showing a user;
    every rejection says what was wrong rather than just failing.
    """
    size = len(data)
    if size < MIN_IMAGE_BYTES:
        raise InvalidImageError(f"File is {size} bytes, too small to be a receipt photo.")
    if size > MAX_IMAGE_BYTES:
        raise InvalidImageError(
            f"File is {size / 1_048_576:.1f} MB, above the {MAX_IMAGE_BYTES // 1_048_576} MB limit."
        )

    try:
        # verify() consumes the file object, so the image is opened twice —
        # once to verify, once to measure and decode.
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            image_format = image.format or ""
            width, height = image.size
            # verify() only checks headers. A JPEG truncated partway through
            # its scan data passes it and then fails later, mid-pipeline,
            # after a receipt row already exists. load() forces a full decode
            # so truncation is caught here instead.
            image.load()
    except UnidentifiedImageError as exc:
        raise InvalidImageError(
            "That file isn't an image we can read. Upload a JPEG, PNG, HEIC, or WEBP."
        ) from exc
    except OSError as exc:  # truncated or corrupt payload
        raise InvalidImageError(f"The image file appears to be damaged: {exc}") from exc

    if image_format not in ALLOWED_FORMATS:
        allowed = ", ".join(sorted(ALLOWED_FORMATS))
        raise InvalidImageError(
            f"{image_format or 'Unknown'} images aren't supported. Use {allowed}."
        )

    if min(width, height) < MIN_EDGE_PIXELS:
        raise InvalidImageError(f"Image is {width}x{height}; too small to read receipt text.")

    return ImageFacts(
        sha256=sha256_hex(data),
        size_bytes=size,
        width=width,
        height=height,
        image_format=image_format,
        extension=ALLOWED_FORMATS[image_format],
    )
