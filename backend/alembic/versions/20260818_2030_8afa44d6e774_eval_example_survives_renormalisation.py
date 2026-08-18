"""eval example survives renormalisation

Normalisation replaces a receipt's line items wholesale. A plain foreign key
from `eval_example.source_line_item_id` turned that into a hard failure: once
a line had been labelled, re-normalising its receipt raised a foreign key
violation, and the guarantee that every stage after extract replays from the
stored extraction stopped holding.

`ON DELETE SET NULL` because the column is provenance, not content. An eval
example stores its own raw text, normalised text, and expected answer, so it
remains a complete label without knowing which line item it came from.


Revision ID: 8afa44d6e774
Revises: cf1e70385be7
Create Date: 2026-08-18 20:30:04.546319+00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "8afa44d6e774"
down_revision: str | None = "cf1e70385be7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Autogenerate proposed creating the new constraint unnamed and dropping it by
# `None` on the way back down, which is not a runnable downgrade. Reusing the
# original name keeps upgrade and downgrade symmetric and leaves the schema
# with the name the model expects.
CONSTRAINT = "eval_example_source_line_item_id_fkey"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "eval_example", type_="foreignkey")
    op.create_foreign_key(
        CONSTRAINT,
        "eval_example",
        "line_item",
        ["source_line_item_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "eval_example", type_="foreignkey")
    op.create_foreign_key(
        CONSTRAINT, "eval_example", "line_item", ["source_line_item_id"], ["id"]
    )
