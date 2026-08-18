"""Stage 1 — ingest.

Stores the image and creates the receipt row *before* anything that can fail
is attempted. Extraction needs the network and a working API key; ingestion
needs neither. A receipt that arrives while the Anthropic API is down is
still safely on disk and queued.

Idempotent on the image content hash: re-uploading the same file returns the
existing receipt rather than creating a second one.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ns.domain.images import ImageFacts, inspect_image
from ns.logging import get_logger
from ns.models import Receipt
from ns.models.base import utcnow
from ns.models.enums import PipelineStatus
from ns.providers.storage import ReceiptStorage, get_storage

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestResult:
    receipt: Receipt
    created: bool  # False when this was a re-upload of known content
    facts: ImageFacts


async def ingest_receipt(
    session: AsyncSession,
    data: bytes,
    *,
    storage: ReceiptStorage | None = None,
) -> IngestResult:
    """Validate, store, and register an uploaded receipt image.

    Raises InvalidImageError if the bytes are not a usable receipt image; the
    caller turns that into a 422 with the message shown to the user.
    """
    facts = inspect_image(data)
    store = storage or get_storage()

    existing = (
        await session.execute(select(Receipt).where(Receipt.image_sha256 == facts.sha256))
    ).scalar_one_or_none()

    if existing is not None:
        # Idempotent re-upload. The row is untouched — in particular the
        # pipeline status and any corrections downstream of it stand, because
        # the bytes are identical and nothing about the receipt has changed.
        #
        # The blob is still checked and rewritten if absent. A row can outlive
        # its image: a database restored from backup alongside an empty
        # storage volume leaves rows pointing at files that do not exist.
        # Re-uploading the original file is the natural way to repair that,
        # and returning early without writing would silently refuse to.
        if not store.exists(existing.image_path):
            log.warning(
                "ingest.blob_missing_restored",
                receipt_id=existing.id,
                key=existing.image_path,
            )
            store.write(facts.sha256, facts.extension, data)

        log.info(
            "ingest.duplicate_content",
            receipt_id=existing.id,
            sha256=facts.sha256[:12],
        )
        return IngestResult(receipt=existing, created=False, facts=facts)

    key = store.write(facts.sha256, facts.extension, data)

    receipt = Receipt(
        image_sha256=facts.sha256,
        image_path=key,
        image_bytes=facts.size_bytes,
        status=PipelineStatus.UPLOADED,
    )
    session.add(receipt)
    await session.flush()

    log.info(
        "ingest.created",
        receipt_id=receipt.id,
        sha256=facts.sha256[:12],
        bytes=facts.size_bytes,
        dimensions=f"{facts.width}x{facts.height}",
        image_format=facts.image_format,
    )
    return IngestResult(receipt=receipt, created=True, facts=facts)


async def find_probable_duplicates(session: AsyncSession, receipt: Receipt) -> list[Receipt]:
    """Receipts that look like a re-photograph of this one (DECISIONS.md D13).

    The content hash only catches the identical *file*. Two photos of one
    receipt are two hashes, so after extraction the (store, date, total)
    triple is checked as well. Matches are surfaced for a one-tap decision and
    never merged automatically — a genuine second trip to the same store on
    the same day for the same amount is unlikely but not impossible, and
    silently merging it would destroy real data.
    """
    if receipt.purchased_at is None or receipt.total_cents is None:
        return []

    rows = await session.execute(
        select(Receipt).where(
            Receipt.id != receipt.id,
            Receipt.store_id == receipt.store_id,
            Receipt.purchased_at == receipt.purchased_at,
            Receipt.total_cents == receipt.total_cents,
        )
    )
    return list(rows.scalars().all())


def touch(receipt: Receipt) -> None:
    receipt.updated_at = utcnow()
