"""Image validation. Pure functions, no database, no filesystem."""

import io

import pytest
from PIL import Image

from ns.domain.images import (
    MAX_IMAGE_BYTES,
    ImageFacts,
    InvalidImageError,
    inspect_image,
    sha256_hex,
)


def make_image(
    width: int = 800, height: int = 1200, fmt: str = "JPEG", color: str = "white"
) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format=fmt)
    return buffer.getvalue()


def test_hash_is_stable_and_content_addressed() -> None:
    a = make_image(color="white")
    assert sha256_hex(a) == sha256_hex(a)
    assert len(sha256_hex(a)) == 64
    assert sha256_hex(a) != sha256_hex(make_image(color="black"))


def test_inspect_reports_dimensions_and_format() -> None:
    facts = inspect_image(make_image(640, 960))
    assert isinstance(facts, ImageFacts)
    assert (facts.width, facts.height) == (640, 960)
    assert facts.image_format == "JPEG"
    assert facts.extension == "jpg"


def test_extension_comes_from_content_not_filename() -> None:
    """A client can name a PNG `receipt.jpg`; the bytes decide."""
    assert inspect_image(make_image(fmt="PNG")).extension == "png"


def test_non_image_bytes_are_rejected_with_a_usable_message() -> None:
    payload = b"MZ\x90\x00" + b"\x00" * 4096  # a DOS/PE header, not an image
    with pytest.raises(InvalidImageError, match="isn't an image we can read"):
        inspect_image(payload)


def test_truncated_image_is_rejected() -> None:
    data = make_image()
    with pytest.raises(InvalidImageError):
        inspect_image(data[: len(data) // 3])


def test_tiny_file_is_rejected() -> None:
    with pytest.raises(InvalidImageError, match="too small to be a receipt"):
        inspect_image(b"\xff\xd8\xff")


def test_oversized_file_is_rejected_before_decoding() -> None:
    with pytest.raises(InvalidImageError, match="above the"):
        inspect_image(b"\x00" * (MAX_IMAGE_BYTES + 1))


def test_low_resolution_image_is_rejected() -> None:
    """A thumbnail cannot carry legible receipt text; failing here is kinder
    than failing later with an empty extraction."""
    with pytest.raises(InvalidImageError, match="too small to read receipt text"):
        inspect_image(make_image(80, 400))


def test_unsupported_format_is_rejected() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (600, 900), "white").save(buffer, format="BMP")
    with pytest.raises(InvalidImageError, match="aren't supported"):
        inspect_image(buffer.getvalue())
