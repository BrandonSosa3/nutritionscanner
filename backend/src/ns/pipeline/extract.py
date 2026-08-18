"""Stage 2 — extract.

Sends the stored receipt image to Claude with vision and records the returned
transcription verbatim in `Receipt.raw_extraction`, permanently. Every later
stage replays from that record, so extraction is paid for once per receipt no
matter how many times normalisation, reconciliation, or resolution change.

Extraction never fails destructively. If the API is unavailable, the key is
missing, or the budget is exhausted, the receipt keeps its image and moves to
`extract_failed`, which is retryable by hand or by the worker.
"""

import base64
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ns.logging import get_logger
from ns.models import Receipt
from ns.models.base import utcnow
from ns.models.enums import LlmStage, PipelineStatus
from ns.providers.anthropic.client import CallResult, call_structured, load_prompt
from ns.providers.anthropic.schemas import ExtractedReceipt
from ns.providers.storage import ReceiptStorage, get_storage

log = get_logger(__name__)

PROMPT_NAME = "extract_v1"

# Media types the Anthropic vision API accepts, keyed by our stored extension.
_MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "heic": "image/heic",
}


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    receipt: Receipt
    extraction: ExtractedReceipt
    call: CallResult[ExtractedReceipt]


def _media_type_for(image_key: str) -> str:
    extension = image_key.rsplit(".", 1)[-1].lower()
    try:
        return _MEDIA_TYPES[extension]
    except KeyError as exc:
        raise ValueError(f"Cannot send {extension!r} images to the vision API.") from exc


async def extract_receipt(
    session: AsyncSession,
    receipt: Receipt,
    *,
    storage: ReceiptStorage | None = None,
    force: bool = False,
) -> ExtractionOutcome:
    """Transcribe a stored receipt image into `raw_extraction`.

    Idempotent by default: a receipt that already has an extraction is not
    re-sent, because doing so costs money and produces no new information.
    Pass `force=True` to re-extract deliberately, which is how a prompt
    revision gets evaluated against a receipt already on file.
    """
    if receipt.raw_extraction is not None and not force:
        raise ValueError(
            f"Receipt {receipt.id} already has an extraction. Pass force=True to re-run it."
        )

    store = storage or get_storage()
    image_bytes = store.read(receipt.image_path)
    media_type = _media_type_for(receipt.image_path)

    receipt.status = PipelineStatus.EXTRACTING
    receipt.updated_at = utcnow()
    await session.flush()

    content: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(image_bytes).decode(),
            },
        },
        {
            "type": "text",
            "text": (
                "Transcribe this grocery receipt. Record what is printed; leave "
                "anything unreadable or ambiguous null and explain it in the notes."
            ),
        },
    ]

    prompt = load_prompt(PROMPT_NAME)

    try:
        call = await call_structured(
            session,
            stage=LlmStage.EXTRACT,
            prompt=prompt,
            content=content,
            output_model=ExtractedReceipt,
            receipt_id=receipt.id,
        )
    except Exception as exc:
        # The image is already stored, so nothing is lost. The receipt is left
        # in a state the worker and the UI can both retry from.
        receipt.status = PipelineStatus.EXTRACT_FAILED
        receipt.updated_at = utcnow()
        await session.flush()
        log.error(
            "extract.failed",
            receipt_id=receipt.id,
            error=type(exc).__name__,
            detail=str(exc)[:200],
        )
        raise

    extraction = call.parsed

    # Stored verbatim. Downstream stages parse from this; none of it is
    # interpreted here.
    receipt.raw_extraction = extraction.model_dump(mode="json")
    receipt.extraction_model = call.model
    receipt.extraction_prompt_version = call.prompt_version
    receipt.extracted_at = utcnow()
    receipt.currency = extraction.currency or receipt.currency
    receipt.status = PipelineStatus.EXTRACTED  # transcribed; normalisation is next
    receipt.updated_at = utcnow()
    await session.flush()

    log.info(
        "extract.completed",
        receipt_id=receipt.id,
        store=extraction.store_name,
        purchased_at=extraction.purchased_at,
        line_items=len(extraction.line_items),
        legibility=extraction.legibility,
        cost_usd=str(call.cost_usd),
        latency_ms=call.latency_ms,
    )

    return ExtractionOutcome(receipt=receipt, extraction=extraction, call=call)
