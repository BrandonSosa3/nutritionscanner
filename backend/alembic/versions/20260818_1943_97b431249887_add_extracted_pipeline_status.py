"""add extracted pipeline status

Extraction had been marking receipts `normalized`, because the enum had no
state between "transcribed" and "line items built". A receipt could therefore
report itself normalised while having no line items at all, which is a lie the
UI would have repeated. `extracted` is that missing state.

Pipeline statuses are stored as VARCHAR with a CHECK constraint rather than a
native Postgres enum (see models/enums.py), so adding a value is exactly this:
an ordinary migration that rewrites the constraint, inside a transaction.
Autogenerate does not detect CHECK constraint changes, so this is hand-written
by design rather than by omission.

Revision ID: 97b431249887
Revises: f9e80d16951e
Create Date: 2026-08-18 19:43:58.201206+00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "97b431249887"
down_revision: str | None = "f9e80d16951e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "pipelinestatus"

_BEFORE = (
    "uploaded",
    "extracting",
    "extract_failed",
    "normalized",
    "reconciled",
    "resolving",
    "needs_review",
    "complete",
)
_AFTER = (
    "uploaded",
    "extracting",
    "extract_failed",
    "extracted",
    "normalized",
    "reconciled",
    "resolving",
    "needs_review",
    "complete",
)


def _values(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "receipt", type_="check")
    op.create_check_constraint(CONSTRAINT, "receipt", f"status IN ({_values(_AFTER)})")


def downgrade() -> None:
    # Receipts sitting in the state being removed are moved back to the status
    # they would have had before it existed. Failing instead would make the
    # downgrade untestable, and silently leaving them would violate the
    # constraint being recreated.
    op.execute("UPDATE receipt SET status = 'normalized' WHERE status = 'extracted'")
    op.drop_constraint(CONSTRAINT, "receipt", type_="check")
    op.create_check_constraint(CONSTRAINT, "receipt", f"status IN ({_values(_BEFORE)})")
