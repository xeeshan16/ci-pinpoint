from __future__ import annotations

from ci_pinpoint.models import TokenUsage
from ci_pinpoint.pricing import FREE, price_for


def test_env_override_wins_over_table_and_free_providers() -> None:
    env = {"CI_PINPOINT_PRICE_IN": "1.0", "CI_PINPOINT_PRICE_OUT": "2.0"}
    price = price_for("ollama", "qwen2.5:3b", env)
    assert price is not None
    assert price.cost(TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)) == 3.0


def test_local_providers_are_free() -> None:
    assert price_for("ollama", "qwen2.5:3b", {}) is FREE
    assert price_for("fake", "fake-diagnoser", {}) is FREE


def test_unknown_hosted_model_has_no_price() -> None:
    assert price_for("openai-compatible", "some-private-model", {}) is None


def test_longest_matching_key_wins() -> None:
    mini = price_for("openai", "gpt-4.1-mini", {})
    full = price_for("openai", "gpt-4.1", {})
    assert mini is not None
    assert full is not None
    assert mini.input_per_mtok < full.input_per_mtok


def test_cost_scales_with_tokens() -> None:
    price = price_for("openai", "gpt-4o-mini", {})
    assert price is not None
    usage = TokenUsage(input_tokens=500_000, output_tokens=250_000)
    # 0.5M * 0.15 + 0.25M * 0.60 = 0.075 + 0.15 = 0.225
    assert round(price.cost(usage), 6) == 0.225


def test_bad_override_is_ignored() -> None:
    assert price_for("openai", "gpt-4o", {"CI_PINPOINT_PRICE_IN": "oops"}) is not None


def test_usage_subtraction_is_non_negative() -> None:
    a = TokenUsage(input_tokens=10, output_tokens=5)
    b = TokenUsage(input_tokens=4, output_tokens=9)
    delta = a - b
    assert delta.input_tokens == 6
    assert delta.output_tokens == 0
    assert delta.total == 6
