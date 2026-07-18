"""Token accounting contracts used before and after LLM calls."""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from numbers import Real
from typing import Literal

CountSource = Literal["reported", "tokenizer", "estimated", "unknown"]
CostKind = Literal["reported", "estimated", "unknown"]
TokenCounter = Callable[[str], int]


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class ContextBudget:
    model: str | None
    max_input_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int

    def __post_init__(self) -> None:
        if not _is_nonnegative_int(self.max_input_tokens) or self.max_input_tokens == 0:
            raise ValueError("max_input_tokens must be a positive integer")
        for name in ("reserved_output_tokens", "safety_margin_tokens"):
            if not _is_nonnegative_int(getattr(self, name)):
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.reserved_output_tokens + self.safety_margin_tokens >= self.max_input_tokens:
            raise ValueError("budget must leave a positive available input")

    @property
    def available_input_tokens(self) -> int:
        return (
            self.max_input_tokens
            - self.reserved_output_tokens
            - self.safety_margin_tokens
        )


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    duration_ms: int | None = None
    estimated_cost: float | None = None
    cost_kind: CostKind = "unknown"

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "duration_ms",
        ):
            value = getattr(self, name)
            if value is not None and not _is_nonnegative_int(value):
                raise ValueError(f"{name} must be a nonnegative integer or None")
        cost = self.estimated_cost
        if cost is not None and (
            not isinstance(cost, Real)
            or isinstance(cost, bool)
            or not math.isfinite(float(cost))
            or cost < 0
        ):
            raise ValueError("estimated_cost must be a nonnegative finite number or None")
        if self.cost_kind not in {"reported", "estimated", "unknown"}:
            raise ValueError("cost_kind is invalid")
        if (cost is None) != (self.cost_kind == "unknown"):
            raise ValueError("cost_kind must describe an existing monetary cost")


@dataclass(frozen=True)
class TokenCount:
    tokens: int | None = None
    source: CountSource = "unknown"

    def __post_init__(self) -> None:
        if self.source not in {"reported", "tokenizer", "estimated", "unknown"}:
            raise ValueError("source is invalid")
        if self.tokens is not None and not _is_nonnegative_int(self.tokens):
            raise ValueError("tokens must be a nonnegative integer or None")
        if (self.tokens is None) != (self.source == "unknown"):
            raise ValueError("unknown counts must not claim a token value")


def count_tokens(
    text: str,
    *,
    model: str | None = None,
    adapters: Mapping[str, TokenCounter] | None = None,
) -> TokenCount:
    """Count with a model adapter, otherwise estimate as one token per UTF-8 byte.

    The byte estimate is deliberately conservative for planning, but is not a
    tokenizer-independent upper-bound guarantee.
    """
    if not isinstance(text, str):
        return TokenCount()
    if text == "":
        return TokenCount(0, "estimated")
    adapter = adapters.get(model) if adapters is not None and model is not None else None
    if adapter is not None:
        try:
            count = adapter(text)
        except Exception:  # noqa: BLE001 - adapters are an optional isolation boundary
            return TokenCount()
        if not _is_nonnegative_int(count):
            return TokenCount()
        return TokenCount(count, "tokenizer")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        return TokenCount()
    return TokenCount(len(encoded), "estimated")


def fits_within_budget(count: TokenCount, budget: ContextBudget) -> bool:
    """Conservatively accept only known counts within the budget's safe input."""
    return count.tokens is not None and count.tokens <= budget.available_input_tokens
