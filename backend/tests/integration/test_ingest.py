"""Ingestion against a real database and a real filesystem.

Includes the five fixture receipts, so this is the first test that proves the
system handles actual photographs rather than synthetic images.
"""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ns.domain.images import InvalidImageError, sha256_hex
from ns.models import Receipt, Store
from ns.models.enums import PipelineStatus
from ns.pipeline.ingest import find_probable_duplicates, ingest_receipt
from ns.providers.storage import LocalReceiptStorage
from tests.conftest import RECEIPT_FIXTURES
from tests.unit.test_images import make_image

pytestmark = pytest.mark.integration


@pytest.fixture
def storage(tmp_path: Path) -> LocalReceiptStorage:
    return LocalReceiptStorage(root=tmp_path / "receipts")


async def test_ingest_creates_a_receipt_and_stores_the_image(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    data = make_image()

    result = await ingest_receipt(session, data, storage=storage)

    assert result.created is True
    assert result.receipt.id is not None
    assert result.receipt.status is PipelineStatus.UPLOADED
    assert result.receipt.image_sha256 == sha256_hex(data)
    assert result.receipt.image_bytes == len(data)
    # The stored bytes are byte-identical to what was uploaded.
    assert storage.read(result.receipt.image_path) == data


async def test_reupload_of_identical_bytes_is_idempotent(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Ingestion is idempotent on image hash — re-upload updates, never duplicates."""
    data = make_image()

    first = await ingest_receipt(session, data, storage=storage)
    second = await ingest_receipt(session, data, storage=storage)

    assert second.created is False
    assert second.receipt.id == first.receipt.id

    count = len((await session.execute(select(Receipt))).all())
    assert count == 1


async def test_different_images_create_separate_receipts(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    a = await ingest_receipt(session, make_image(color="white"), storage=storage)
    b = await ingest_receipt(session, make_image(color="black"), storage=storage)

    assert a.receipt.id != b.receipt.id
    assert a.receipt.image_path != b.receipt.image_path


async def test_invalid_image_is_rejected_before_any_row_is_written(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Never persist a receipt we could not even decode."""
    with pytest.raises(InvalidImageError):
        await ingest_receipt(session, b"not an image at all" * 100, storage=storage)

    assert len((await session.execute(select(Receipt))).all()) == 0


async def test_storage_path_is_derived_from_content_hash(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Nothing a client sends influences where a file lands on disk."""
    data = make_image()
    result = await ingest_receipt(session, data, storage=storage)

    digest = sha256_hex(data)
    assert result.receipt.image_path == f"{digest[:2]}/{digest}.jpg"


async def test_storage_rejects_keys_that_escape_the_root(
    storage: LocalReceiptStorage,
) -> None:
    with pytest.raises(ValueError, match="escapes the storage root"):
        storage.read("../../etc/passwd")


# ── The real receipts ─────────────────────────────────────────────────────

FIXTURE_NAMES = [
    "01-au-produce.png",
    "02-us-wholefoods.png",
    "03-za-spar.png",
    "04-us-costco.png",
    "05-us-sprouts.jpg",
]


@pytest.mark.parametrize("name", FIXTURE_NAMES)
async def test_real_receipt_photographs_ingest(
    session: AsyncSession, storage: LocalReceiptStorage, name: str
) -> None:
    """Every fixture receipt survives validation and ingestion.

    `05-us-sprouts.jpg` matters most here: it is a 2576x1932 phone photo,
    rotated and heavily creased. If image validation is too strict, this is
    the one it wrongly rejects.
    """
    path = RECEIPT_FIXTURES / name
    if not path.is_file():
        pytest.skip(f"fixture {name} not present")

    data = path.read_bytes()
    result = await ingest_receipt(session, data, storage=storage)

    assert result.created is True
    assert result.facts.width >= 200
    assert result.facts.height >= 200
    assert storage.read(result.receipt.image_path) == data


async def test_all_fixtures_have_distinct_hashes(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    present = [p for name in FIXTURE_NAMES if (p := RECEIPT_FIXTURES / name).is_file()]
    if len(present) < 2:
        pytest.skip("need at least two fixtures present")

    hashes = set()
    for path in present:
        result = await ingest_receipt(session, path.read_bytes(), storage=storage)
        hashes.add(result.receipt.image_sha256)

    assert len(hashes) == len(present), "fixtures should be distinct images"


# ── D13: duplicate detection beyond the content hash ──────────────────────


async def test_rephotographed_receipt_is_flagged_not_merged(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """Two photos of one receipt are two hashes.

    The (store, date, total) triple catches what the content hash cannot.
    Matches are surfaced for a decision, never merged automatically.
    """
    store = Store(name="Sprouts")
    session.add(store)
    await session.flush()

    first = (await ingest_receipt(session, make_image(color="white"), storage=storage)).receipt
    second = (await ingest_receipt(session, make_image(color="black"), storage=storage)).receipt

    for receipt in (first, second):
        receipt.store_id = store.id
        receipt.purchased_at = date(2026, 8, 15)
        receipt.total_cents = 1108
    await session.flush()

    matches = await find_probable_duplicates(session, second)

    assert [m.id for m in matches] == [first.id]
    # Flagged only — nothing was deleted or merged.
    assert second.duplicate_of_receipt_id is None
    assert len((await session.execute(select(Receipt))).all()) == 2


async def test_duplicate_check_is_inert_before_extraction(
    session: AsyncSession, storage: LocalReceiptStorage
) -> None:
    """With no date or total yet, every receipt would otherwise match."""
    receipt = (await ingest_receipt(session, make_image(), storage=storage)).receipt
    assert await find_probable_duplicates(session, receipt) == []
