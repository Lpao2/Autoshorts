"""Centralized Gemini pricing and local budget policy."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_MONTHLY_BUDGET_USD = 8.0


@dataclass(frozen=True, slots=True)
class TokenPrice:
    input_per_million: float
    output_per_million: float


# Introductory Gemini 3.8 Flash pricing through 2026-12-31. Thinking tokens
# are billed as output tokens by Google.
MODEL_PRICES = {
    "gemini-3.8-flash": TokenPrice(0.75, 3.75),
    "gemini-3.5-flash-lite": TokenPrice(0.30, 2.50),
}


def estimate_cost_usd(
    model: str, input_tokens: int, billed_output_tokens: int
) -> float:
    price = MODEL_PRICES.get(model)
    if price is None:
        raise ValueError(f"Preço local não configurado para o modelo {model}.")
    return (
        input_tokens * price.input_per_million
        + billed_output_tokens * price.output_per_million
    ) / 1_000_000


def monthly_budget_usd() -> float:
    raw = os.environ.get("AUTOSHORTS_GEMINI_MONTHLY_BUDGET_USD", "8.00")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            "AUTOSHORTS_GEMINI_MONTHLY_BUDGET_USD deve ser numérico."
        ) from exc
    if value < 0:
        raise ValueError("O limite mensal de IA não pode ser negativo.")
    return value
