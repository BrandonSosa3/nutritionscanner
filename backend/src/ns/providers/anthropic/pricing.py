"""Model pricing and cost computation.

Prices are USD per million tokens, as published by Anthropic. They are kept
here rather than inferred, because every LLM call records its cost at the time
it was made (a brief requirement) and a wrong constant would silently
mis-state months of history.

If a model is missing, cost computation raises rather than returning zero. A
silent zero would make the budget guard useless precisely when a new model is
introduced.
"""

from dataclasses import dataclass
from decimal import Decimal

_MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_per_mtok: Decimal
    output_per_mtok: Decimal

    @property
    def cache_write_per_mtok(self) -> Decimal:
        """Writing to the prompt cache costs 1.25x the base input rate."""
        return self.input_per_mtok * Decimal("1.25")

    @property
    def cache_read_per_mtok(self) -> Decimal:
        """Reading from the prompt cache costs 0.1x the base input rate."""
        return self.input_per_mtok * Decimal("0.10")


# Verified against Anthropic's published pricing. Update deliberately, and
# note that historical LlmCall rows keep the cost recorded at the time.
PRICING: dict[str, ModelPricing] = {
    "claude-opus-5": ModelPricing(Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-8": ModelPricing(Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": ModelPricing(Decimal("3.00"), Decimal("15.00")),
    "claude-sonnet-4-6": ModelPricing(Decimal("3.00"), Decimal("15.00")),
    "claude-haiku-4-5": ModelPricing(Decimal("1.00"), Decimal("5.00")),
}


class UnknownModelError(KeyError):
    """No pricing on file for this model, so cost cannot be computed."""


def pricing_for(model: str) -> ModelPricing:
    try:
        return PRICING[model]
    except KeyError as exc:
        known = ", ".join(sorted(PRICING))
        raise UnknownModelError(
            f"No pricing recorded for model {model!r}. Add it to pricing.py. Known: {known}"
        ) from exc


def compute_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    """Exact cost for one call, to six decimal places.

    Decimal rather than float throughout: these values accumulate into a
    monthly total that gates further spending.
    """
    p = pricing_for(model)
    total = (
        Decimal(input_tokens) * p.input_per_mtok
        + Decimal(output_tokens) * p.output_per_mtok
        + Decimal(cache_read_tokens) * p.cache_read_per_mtok
        + Decimal(cache_write_tokens) * p.cache_write_per_mtok
    ) / _MILLION
    return total.quantize(Decimal("0.000001"))


def estimate_cost_usd(model: str, *, input_tokens: int, expected_output_tokens: int) -> Decimal:
    """Pre-flight estimate, used by the budget guard before a call is made."""
    return compute_cost_usd(model, input_tokens=input_tokens, output_tokens=expected_output_tokens)
