"""API response models.

Deliberately separate from the SQLModel tables. Receipt images are personal
financial records, so `image_path` is never serialised — the client refers to
an image by receipt id and fetches it through an endpoint, and a storage key
never appears in a response body or a log line.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from ns.models import Receipt
from ns.models.enums import PipelineStatus, ReconciliationStatus


class ReceiptSummary(BaseModel):
    """A receipt as it appears in a list."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: PipelineStatus
    reconciliation_status: ReconciliationStatus
    store_id: int | None
    purchased_at: date | None
    total_cents: int | None
    currency: str
    uploaded_at: datetime

    @classmethod
    def of(cls, receipt: Receipt) -> "ReceiptSummary":
        return cls.model_validate(receipt)


class ReceiptDetail(ReceiptSummary):
    """A single receipt, with the reconciliation evidence the UI needs."""

    subtotal_cents: int | None
    tax_cents: int | None
    reconciliation_delta_cents: int | None
    reconciliation_report: dict[str, object] | None
    extraction_model: str | None
    extracted_at: datetime | None
    duplicate_of_receipt_id: int | None
    image_bytes: int
    # Content hash is safe to expose and lets a client tell whether a local
    # file has already been uploaded before sending it.
    image_sha256: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, receipt: Receipt) -> "ReceiptDetail":
        return cls.model_validate(receipt)


class ReceiptUploadResponse(BaseModel):
    """The result of an upload.

    `created` distinguishes a new receipt from an idempotent re-upload, so the
    UI can say "already uploaded on 12 August" instead of implying a second
    receipt was recorded.
    """

    receipt: ReceiptDetail
    created: bool
    width: int
    height: int
    image_format: str


class ReceiptListResponse(BaseModel):
    items: list[ReceiptSummary]
    total: int
    limit: int
    offset: int


class ErrorResponse(BaseModel):
    """Errors state what happened and what to do about it (see DESIGN.md)."""

    detail: str = Field(description="Human-readable, safe to show a user verbatim.")
