"""Cost computation. Pure arithmetic, no API calls."""

from decimal import Decimal

import pytest

from ns.providers.anthropic.pricing import (
    UnknownModelError,
    compute_cost_usd,
    pricing_for,
)


def test_cost_matches_published_rates() -> None:
    """Opus 5: $5 per million input, $25 per million output."""
    assert compute_cost_usd("claude-opus-5", input_tokens=1_000_000, output_tokens=0) == Decimal(
        "5.000000"
    )
    assert compute_cost_usd("claude-opus-5", input_tokens=0, output_tokens=1_000_000) == Decimal(
        "25.000000"
    )


def test_cache_reads_are_a_tenth_of_input() -> None:
    full = compute_cost_usd("claude-opus-5", input_tokens=1_000_000, output_tokens=0)
    cached = compute_cost_usd(
        "claude-opus-5", input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000
    )
    assert cached == full / 10


def test_cache_writes_carry_a_premium() -> None:
    full = compute_cost_usd("claude-opus-5", input_tokens=1_000_000, output_tokens=0)
    written = compute_cost_usd(
        "claude-opus-5", input_tokens=0, output_tokens=0, cache_write_tokens=1_000_000
    )
    assert written == full * Decimal("1.25")


def test_a_realistic_receipt_extraction_costs_about_nine_cents() -> None:
    """Guards the estimate this project's budgeting is based on."""
    cost = compute_cost_usd("claude-opus-5", input_tokens=6000, output_tokens=2500)
    assert Decimal("0.05") < cost < Decimal("0.15")


def test_caching_reduces_the_cost_of_a_repeated_prompt() -> None:
    uncached = compute_cost_usd("claude-opus-5", input_tokens=6000, output_tokens=2500)
    cached = compute_cost_usd(
        "claude-opus-5", input_tokens=800, output_tokens=2500, cache_read_tokens=5200
    )
    assert cached < uncached


def test_cheaper_models_are_cheaper() -> None:
    """The eval harness compares these; the ordering must hold."""
    args = {"input_tokens": 6000, "output_tokens": 2500}
    opus = compute_cost_usd("claude-opus-5", **args)
    sonnet = compute_cost_usd("claude-sonnet-5", **args)
    haiku = compute_cost_usd("claude-haiku-4-5", **args)
    assert haiku < sonnet < opus


def test_unknown_model_raises_rather_than_returning_zero() -> None:
    """A silent zero would disable the budget guard exactly when a new model
    is introduced."""
    with pytest.raises(UnknownModelError, match="No pricing recorded"):
        compute_cost_usd("claude-not-a-real-model", input_tokens=1, output_tokens=1)


def test_zero_usage_costs_nothing() -> None:
    assert compute_cost_usd("claude-opus-5", input_tokens=0, output_tokens=0) == Decimal("0")


def test_pricing_lookup_lists_known_models_on_failure() -> None:
    with pytest.raises(UnknownModelError, match="claude-opus-5"):
        pricing_for("nope")
