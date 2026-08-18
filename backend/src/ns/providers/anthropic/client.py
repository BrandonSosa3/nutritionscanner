"""Anthropic client wrapper.

Every call through here is budget-checked before it is made and recorded
afterwards with model, tokens, latency, and cost — a brief requirement, and
the data the eval harness compares prompt revisions against.

Prompts live in `prompts/` as files and are identified by a content hash, so a
ResolverRun can be attributed to the exact prompt text that produced it. A
prompt edited in place gets a new version automatically.
"""

import hashlib
import time
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from anthropic import AsyncAnthropic
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ns.config import get_settings
from ns.logging import get_logger
from ns.models import LlmCall
from ns.models.enums import LlmStage
from ns.providers.anthropic.budget import assert_within_budget
from ns.providers.anthropic.pricing import compute_cost_usd, estimate_cost_usd

log = get_logger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


class MissingApiKeyError(RuntimeError):
    """No ANTHROPIC_API_KEY configured."""


@dataclass(frozen=True, slots=True)
class Prompt:
    name: str
    text: str

    @property
    def version(self) -> str:
        """Short content hash. Editing the prompt changes it automatically."""
        return hashlib.sha256(self.text.encode()).hexdigest()[:12]


@lru_cache
def load_prompt(name: str) -> Prompt:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        available = ", ".join(sorted(p.stem for p in PROMPTS_DIR.glob("*.md")))
        raise FileNotFoundError(f"No prompt named {name!r}. Available: {available}")
    return Prompt(name=name, text=path.read_text(encoding="utf-8"))


def get_client() -> AsyncAnthropic:
    settings = get_settings()
    if settings.anthropic_api_key is None:
        raise MissingApiKeyError(
            "ANTHROPIC_API_KEY is not set. Add it to .env — see .env.example. "
            "Receipts already uploaded are safe and will process once it is configured."
        )
    return AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())


@dataclass(frozen=True, slots=True)
class CallResult[TModel: BaseModel]:
    parsed: TModel
    cost_usd: Decimal
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    stop_reason: str | None
    model: str
    prompt_version: str


async def call_structured[TModel: BaseModel](
    session: AsyncSession,
    *,
    stage: LlmStage,
    prompt: Prompt,
    content: list[dict[str, Any]],
    output_model: type[TModel],
    receipt_id: int | None = None,
    max_tokens: int = 16_000,
    expected_output_tokens: int = 3_000,
) -> CallResult[TModel]:
    """Make one structured-output call, budget-checked and fully recorded.

    Raises BudgetExceededError before spending anything if the estimated cost
    would cross the configured monthly ceiling.
    """
    settings = get_settings()
    model = settings.anthropic_model

    # Rough pre-flight estimate. Exact input tokens are unknown until the
    # request is built, but the guard only needs to be approximately right to
    # stop a runaway loop.
    estimate = estimate_cost_usd(
        model,
        input_tokens=8_000,
        expected_output_tokens=expected_output_tokens,
    )
    await assert_within_budget(session, estimate)

    client = get_client()
    started = time.perf_counter()
    error_type: str | None = None

    try:
        response = await client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": prompt.text,
                    # The prompt is identical on every receipt, so it reads
                    # from cache at a tenth of the input price after the first
                    # call.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": cast(Any, content)}],
            output_format=output_model,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        error_type = type(exc).__name__
        session.add(
            LlmCall(
                receipt_id=receipt_id,
                stage=stage,
                model=model,
                prompt_version=prompt.version,
                input_tokens=0,
                output_tokens=0,
                latency_ms=latency_ms,
                cost_usd=Decimal(0),
                ok=False,
                error_type=error_type,
            )
        )
        await session.flush()
        log.error("llm.call_failed", stage=stage.value, model=model, error=error_type)
        raise

    latency_ms = int((time.perf_counter() - started) * 1000)
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

    cost = compute_cost_usd(
        model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )

    session.add(
        LlmCall(
            receipt_id=receipt_id,
            stage=stage,
            model=model,
            prompt_version=prompt.version,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            latency_ms=latency_ms,
            cost_usd=cost,
            ok=True,
            stop_reason=response.stop_reason,
        )
    )
    await session.flush()

    log.info(
        "llm.call",
        stage=stage.value,
        model=model,
        prompt_version=prompt.version,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=cache_read,
        latency_ms=latency_ms,
        cost_usd=str(cost),
        receipt_id=receipt_id,
    )

    if response.parsed_output is None:
        raise ValueError(
            f"Model returned no parseable output (stop_reason={response.stop_reason})."
        )

    return CallResult(
        parsed=response.parsed_output,
        cost_usd=cost,
        latency_ms=latency_ms,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        stop_reason=response.stop_reason,
        model=model,
        prompt_version=prompt.version,
    )
