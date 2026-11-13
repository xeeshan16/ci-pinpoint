"""Estimate what a diagnosis costs.

Model calls cost money on hosted providers and nothing on a local one. A
developer deciding between ``gpt-4.1-mini`` and a local ``qwen2.5`` wants the
price of a diagnosis in the brief, not a surprise on their bill.

The built-in table holds *indicative published list prices* in USD per million
tokens, entered by hand and easy to fall out of date. They are a convenience,
not a quote: set ``CI_PINPOINT_PRICE_IN`` and ``CI_PINPOINT_PRICE_OUT`` (USD per
million input / output tokens) to price with your own contract, and those
always win. Local providers (Ollama, the fake test provider) are free.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from ci_pinpoint.models import TokenUsage

log = logging.getLogger(__name__)

FREE_PROVIDERS = frozenset({"ollama", "fake"})


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float

    def cost(self, usage: TokenUsage) -> float:
        return (
            usage.input_tokens * self.input_per_mtok + usage.output_tokens * self.output_per_mtok
        ) / 1_000_000


FREE = Price(0.0, 0.0)

# Indicative published list prices (USD / 1M tokens). Matched case-insensitively
# by the longest key that is a substring of the model name, so "gpt-4.1-mini"
# wins over "gpt-4.1". Override per model with the env vars above.
DEFAULT_PRICES: dict[str, Price] = {
    "gpt-4.1-mini": Price(0.40, 1.60),
    "gpt-4.1-nano": Price(0.10, 0.40),
    "gpt-4.1": Price(2.00, 8.00),
    "gpt-4o-mini": Price(0.15, 0.60),
    "gpt-4o": Price(2.50, 10.00),
    "o4-mini": Price(1.10, 4.40),
    "claude-haiku-4-5": Price(0.80, 4.00),
    "claude-3-5-haiku": Price(0.80, 4.00),
    "claude-haiku": Price(0.80, 4.00),
    "claude-sonnet": Price(3.00, 15.00),
    "claude-opus": Price(15.00, 75.00),
}


def _env_price(env: Mapping[str, str]) -> Price | None:
    raw_in = env.get("CI_PINPOINT_PRICE_IN")
    raw_out = env.get("CI_PINPOINT_PRICE_OUT")
    if raw_in is None and raw_out is None:
        return None
    try:
        return Price(float(raw_in or 0.0), float(raw_out or 0.0))
    except ValueError:
        log.warning(
            "ignoring non-numeric price override",
            extra={"price_in": raw_in, "price_out": raw_out},
        )
        return None


def _table_price(model: str) -> Price | None:
    needle = model.casefold()
    best: tuple[int, Price] | None = None
    for key, price in DEFAULT_PRICES.items():
        if key in needle and (best is None or len(key) > best[0]):
            best = (len(key), price)
    return best[1] if best else None


def price_for(provider: str, model: str, env: Mapping[str, str]) -> Price | None:
    """The price for one model, or ``None`` when it is unknown.

    An explicit env override wins; then local providers are free; then the
    built-in table. ``None`` means "don't guess a cost", not "free".
    """
    override = _env_price(env)
    if override is not None:
        return override
    if provider in FREE_PROVIDERS:
        return FREE
    return _table_price(model)
