"""LLM call accounting.

The brief requires tracked cost and latency per receipt. A log line cannot be
joined against a ResolverRun, so this is a table.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Column, Numeric
from sqlmodel import Field, Index, SQLModel

from ns.models.base import enum_column, timestamp_column, utcnow
from ns.models.enums import LlmStage


class LlmCall(SQLModel, table=True):
    __tablename__ = "llm_call"

    id: int | None = Field(default=None, primary_key=True)

    receipt_id: int | None = Field(
        default=None, foreign_key="receipt.id", index=True, ondelete="SET NULL"
    )
    resolver_run_id: int | None = Field(
        default=None, foreign_key="resolver_run.id", index=True, ondelete="SET NULL"
    )

    stage: LlmStage = Field(sa_column=enum_column(LlmStage, nullable=False, index=True))
    model: str = Field(max_length=80)
    prompt_version: str | None = Field(default=None, max_length=64)
    called_at: datetime = Field(default_factory=utcnow, sa_column=timestamp_column(nullable=False))

    input_tokens: int = Field(default=0, nullable=False)
    output_tokens: int = Field(default=0, nullable=False)
    # Caching is a large part of resolution cost; tracked separately so the
    # cache hit rate is visible rather than inferred.
    cache_read_tokens: int = Field(default=0, nullable=False)
    cache_write_tokens: int = Field(default=0, nullable=False)

    latency_ms: int
    # 6 decimal places: a single call can cost fractions of a cent.
    cost_usd: Decimal = Field(sa_column=Column(Numeric(12, 6), nullable=False))

    ok: bool = Field(default=True, nullable=False)
    error_type: str | None = Field(default=None, max_length=100)
    stop_reason: str | None = Field(default=None, max_length=40)

    __table_args__ = (Index("ix_llm_call_stage_time", "stage", "called_at"),)
