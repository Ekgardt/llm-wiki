"""Token accounting contracts used before and after LLM calls."""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
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


@dataclass(frozen=True)
class ContextItem:
    item_id: str
    text: str
    source: str
    priority: int
    relevance: float
    confidence: str
    freshness: str
    token_cost: int
    mandatory: bool
    representation: str

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or not self.item_id:
            raise ValueError("item_id must be a non-empty string")
        if not isinstance(self.text, str):
            raise ValueError("text must be a string")
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("source must be a non-empty string")
        if not _is_nonnegative_int(self.priority):
            raise ValueError("priority must be a nonnegative integer")
        if (
            not isinstance(self.relevance, Real)
            or isinstance(self.relevance, bool)
            or not math.isfinite(float(self.relevance))
            or self.relevance < 0
        ):
            raise ValueError("relevance must be a nonnegative finite number")
        if not isinstance(self.confidence, str) or not self.confidence:
            raise ValueError("confidence must be a non-empty string")
        if not isinstance(self.freshness, str) or not self.freshness:
            raise ValueError("freshness must be a non-empty string")
        if not _is_nonnegative_int(self.token_cost):
            raise ValueError("token_cost must be a nonnegative integer")
        if not isinstance(self.mandatory, bool):
            raise ValueError("mandatory must be a boolean")
        if not isinstance(self.representation, str) or not self.representation:
            raise ValueError("representation must be a non-empty string")


@dataclass(frozen=True)
class DroppedItem:
    item_id: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or not self.item_id:
            raise ValueError("item_id must be a non-empty string")
        if self.reason not in {"budget", "section", "diversity", "emergency_cap"}:
            raise ValueError(f"unknown drop reason: {self.reason!r}")


@dataclass(frozen=True)
class PackedContext:
    items: tuple[ContextItem, ...]
    text: str
    packed_tokens: int
    counter_source: CountSource
    dropped: tuple[DroppedItem, ...]
    budget: ContextBudget
    truncated: bool


class BudgetExceededError(ValueError):
    """Raised when mandatory items cannot fit within the usable budget."""


def _normalize_items(items: Iterable[ContextItem]) -> tuple[ContextItem, ...]:
    seen: set[str] = set()
    normalized: list[ContextItem] = []
    for item in items:
        if not isinstance(item, ContextItem):
            raise TypeError("items must be ContextItem instances")
        if item.item_id in seen:
            raise ValueError(f"duplicate item_id: {item.item_id!r}")
        seen.add(item.item_id)
        normalized.append(item)
    return tuple(normalized)


def _item_costs(
    items: tuple[ContextItem, ...],
    *,
    model: str | None,
    counter: Mapping[str, TokenCounter] | None,
) -> tuple[tuple[int, ...], CountSource]:
    """Return per-item token costs and the counter source label.

    When a tokenizer adapter is available for ``model``, every item's cost is
    recomputed from its text and the source is ``tokenizer``. Otherwise the
    precomputed ``token_cost`` is used and the source is ``estimated`` (the
    conventional precompute used throughout the codebase).
    """
    adapter = counter.get(model) if counter is not None and model is not None else None
    if adapter is None:
        return tuple(item.token_cost for item in items), "estimated"
    costs: list[int] = []
    for item in items:
        try:
            measured = adapter(item.text)
        except Exception:  # noqa: BLE001 - adapters are an optional isolation boundary
            costs.append(item.token_cost)
            continue
        if not _is_nonnegative_int(measured):
            costs.append(item.token_cost)
            continue
        costs.append(measured)
    return tuple(costs), "tokenizer"


def _utility_key(item: ContextItem, cost: int) -> tuple[float, str]:
    """Larger-is-better utility per token, ties broken by item_id ascending."""
    safe_cost = max(cost, 1)
    return (float(item.relevance) / safe_cost, item.item_id)


def pack_context(
    items: Iterable[ContextItem],
    budget: ContextBudget,
    *,
    model: str | None = None,
    counter: Mapping[str, TokenCounter] | None = None,
    section_bounds: Mapping[int, int] | None = None,
    per_source_cap: int | None = None,
    emergency_byte_cap: int | None = None,
) -> PackedContext:
    """Pack complete :class:`ContextItem` instances under a shared budget.

    Algorithm (deterministic):

    1. Recompute token costs via the optional ``counter`` adapter when present,
       otherwise use each item's precomputed ``token_cost``.
    2. Reserve every mandatory item whole. If their sum exceeds the usable
       budget, raise :class:`BudgetExceededError` — the caller must shrink the
       input.
    3. Apply ``section_bounds`` (per-priority-class token caps) and
       ``per_source_cap`` (per-source diversity caps) to optional items,
       dropping the lowest-utility overflow with a labeled reason.
    4. Greedily add remaining optional items by utility-per-token, ties broken
       by ``item_id`` ascending, until the remaining budget is exhausted.
    5. Apply ``emergency_byte_cap`` as a failure guard: if the joined output
       bytes exceed the cap, drop the least-important whole item and emit a
       warning reason. Markdown is never sliced mid-item.

    The returned :class:`PackedContext` reports the packed token count, the
    counter source actually used, every dropped item ID with its reason, and
    whether the emergency cap triggered.
    """
    if not isinstance(budget, ContextBudget):
        raise TypeError("budget must be a ContextBudget")
    if section_bounds is not None:
        normalized_bounds = {
            int(key): (0 if value is None else int(value))
            for key, value in section_bounds.items()
        }
    else:
        normalized_bounds = {}
    if per_source_cap is not None and (not _is_nonnegative_int(per_source_cap)):
        raise ValueError("per_source_cap must be a nonnegative integer")
    if emergency_byte_cap is not None and not _is_nonnegative_int(emergency_byte_cap):
        raise ValueError("emergency_byte_cap must be a nonnegative integer")

    normalized = _normalize_items(items)
    costs, counter_source = _item_costs(
        normalized, model=model, counter=counter
    )
    cost_by_id = {item.item_id: cost for item, cost in zip(normalized, costs)}

    dropped: list[DroppedItem] = []
    mandatory: list[ContextItem] = []
    optional: list[ContextItem] = []
    for item in normalized:
        if item.mandatory:
            mandatory.append(item)
        else:
            optional.append(item)

    mandatory_tokens = sum(cost_by_id[i.item_id] for i in mandatory)
    if mandatory_tokens > budget.available_input_tokens:
        raise BudgetExceededError(
            "mandatory items require "
            f"{mandatory_tokens} tokens but the budget leaves "
            f"{budget.available_input_tokens}"
        )

    # Section caps: drop optional items whose priority section is already full.
    section_used: dict[int, int] = {}
    section_survivors: list[ContextItem] = []
    for item in optional:
        cap = normalized_bounds.get(item.priority) if normalized_bounds else None
        if cap is not None:
            used = section_used.get(item.priority, 0)
            cost = cost_by_id[item.item_id]
            if used + cost > cap:
                dropped.append(DroppedItem(item.item_id, "section"))
                continue
            section_used[item.priority] = used + cost
        section_survivors.append(item)
    optional = section_survivors

    # Per-source diversity caps: keep the highest-utility items per source.
    if per_source_cap is not None:
        by_source: dict[str, list[ContextItem]] = {}
        for item in optional:
            by_source.setdefault(item.source, []).append(item)
        kept_ids: set[str] = set()
        for source_items in by_source.values():
            source_items.sort(
                key=lambda it: _utility_key(it, cost_by_id[it.item_id]), reverse=True
            )
            for kept in source_items[:per_source_cap]:
                kept_ids.add(kept.item_id)
        new_optional: list[ContextItem] = []
        for item in optional:
            if item.item_id in kept_ids:
                new_optional.append(item)
            else:
                dropped.append(DroppedItem(item.item_id, "diversity"))
        optional = new_optional

    # Greedy add by utility per token.
    ranked = sorted(
        optional,
        key=lambda it: _utility_key(it, cost_by_id[it.item_id]),
        reverse=True,
    )
    remaining = budget.available_input_tokens - mandatory_tokens
    kept_optional: list[ContextItem] = []
    for item in ranked:
        cost = cost_by_id[item.item_id]
        if cost > remaining:
            dropped.append(DroppedItem(item.item_id, "budget"))
            continue
        remaining -= cost
        kept_optional.append(item)

    # Final output order: priority ascending (safety first), ties by item_id.
    packed_items = sorted(
        [*mandatory, *kept_optional],
        key=lambda it: (it.priority, it.item_id),
    )

    text = "\n\n".join(item.text for item in packed_items)
    truncated = False
    if emergency_byte_cap is not None and len(text.encode("utf-8")) > emergency_byte_cap:
        # Drop whole items from the tail (least important) until it fits.
        # Mandatory items are never dropped here — they were validated above.
        while packed_items and len(text.encode("utf-8")) > emergency_byte_cap:
            victim = packed_items.pop()
            text = "\n\n".join(item.text for item in packed_items)
            truncated = True
            dropped.append(DroppedItem(victim.item_id, "emergency_cap"))

    packed_tokens = sum(cost_by_id[i.item_id] for i in packed_items)
    return PackedContext(
        items=tuple(packed_items),
        text=text,
        packed_tokens=packed_tokens,
        counter_source=counter_source,
        dropped=tuple(dropped),
        budget=budget,
        truncated=truncated,
    )
