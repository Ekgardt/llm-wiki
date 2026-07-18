"""Token accounting contracts used before and after LLM calls."""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from numbers import Real
from typing import Literal

CountSource = Literal["reported", "tokenizer", "estimated", "mixed", "unknown"]
CostKind = Literal["reported", "estimated", "unknown"]
TokenCounter = Callable[[str], int]
PriorityClass = Literal[
    "safety",
    "health",
    "handoff",
    "blocker",
    "decision",
    "evidence",
    "history",
]

PRIORITY_CLASS_ORDER: dict[str, int] = {
    "safety": 1,
    "health": 2,
    "handoff": 3,
    "blocker": 4,
    "decision": 5,
    "evidence": 6,
    "history": 7,
}


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


DEFAULT_CONTEXT_BUDGET = ContextBudget(
    model=None,
    max_input_tokens=8192,
    reserved_output_tokens=0,
    safety_margin_tokens=512,
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
        if self.source not in {"reported", "tokenizer", "estimated", "mixed", "unknown"}:
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
    parent_id: str | None = None
    priority_class: PriorityClass = "evidence"

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
        if self.parent_id is not None and (
            not isinstance(self.parent_id, str) or not self.parent_id
        ):
            raise ValueError("parent_id must be a non-empty string or None")
        if self.priority_class not in PRIORITY_CLASS_ORDER:
            raise ValueError("priority_class is invalid")


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


@dataclass(frozen=True)
class BudgetFailure:
    code: str
    mandatory_item_ids: tuple[str, ...]
    required_tokens: int
    available_tokens: int
    model: str | None
    counter_source: CountSource
    required_bytes: int | None = None
    available_bytes: int | None = None

    def render(self) -> str:
        item_ids = ", ".join(self.mandatory_item_ids)
        rendered = (
            "## Context budget failure\n"
            f"- code: `{self.code}`\n"
            f"- mandatory_items: `{item_ids}`\n"
            f"- required_tokens: `{self.required_tokens}`\n"
            f"- available_tokens: `{self.available_tokens}`\n"
            f"- model: `{self.model or 'unspecified'}`\n"
            f"- counter_source: `{self.counter_source}`"
        )
        if self.required_bytes is not None and self.available_bytes is not None:
            rendered += (
                f"\n- required_bytes: `{self.required_bytes}`"
                f"\n- available_bytes: `{self.available_bytes}`"
            )
        return rendered


class BudgetExceededError(ValueError):
    """Raised when mandatory items cannot fit within the usable budget."""

    def __init__(self, failure: BudgetFailure):
        self.failure = failure
        super().__init__(
            "mandatory context cannot fit: "
            f"{failure.required_tokens} > {failure.available_tokens} "
            f"({failure.code})"
        )


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
) -> tuple[tuple[int, ...], tuple[CountSource, ...]]:
    """Return per-item token costs and the counter source label.

    When a tokenizer adapter is available for ``model``, every item's cost is
    recomputed from its text and the source is ``tokenizer``. Otherwise the
    precomputed ``token_cost`` is used and the source is ``estimated`` (the
    conventional precompute used throughout the codebase).
    """
    adapter = counter.get(model) if counter is not None and model is not None else None
    if adapter is None:
        return tuple(item.token_cost for item in items), tuple("estimated" for _ in items)
    costs: list[int] = []
    sources: list[CountSource] = []
    for item in items:
        try:
            measured = adapter(item.text)
        except Exception:  # noqa: BLE001 - adapters are an optional isolation boundary
            costs.append(item.token_cost)
            sources.append("estimated")
            continue
        if not _is_nonnegative_int(measured):
            costs.append(item.token_cost)
            sources.append("estimated")
            continue
        costs.append(measured)
        sources.append("tokenizer")
    return tuple(costs), tuple(sources)


def _utility(item: ContextItem, cost: int) -> float:
    """Return semantic priority plus relevance-per-token utility."""
    safe_cost = max(cost, 1)
    semantic = len(PRIORITY_CLASS_ORDER) - PRIORITY_CLASS_ORDER[item.priority_class]
    return semantic * 1_000_000 + float(item.relevance) / safe_cost


def _ranked(items: Iterable[ContextItem], costs: Mapping[str, int]) -> list[ContextItem]:
    return sorted(items, key=lambda item: (-_utility(item, costs[item.item_id]), item.item_id))


def _render(items: Iterable[ContextItem]) -> str:
    return "\n\n".join(item.text for item in items)


def _measure_rendered(
    text: str,
    *,
    model: str | None,
    counter: Mapping[str, TokenCounter] | None,
) -> tuple[int, CountSource]:
    measured = count_tokens(text, model=model, adapters=counter)
    if measured.tokens is None:
        return len(text.encode("utf-8")), "estimated"
    return measured.tokens, measured.source


def _combined_source(sources: Iterable[CountSource]) -> CountSource:
    used = {source for source in sources if source != "unknown"}
    if not used:
        return "unknown"
    if len(used) == 1:
        return next(iter(used))
    return "mixed"


def pack_context(
    items: Iterable[ContextItem],
    budget: ContextBudget,
    *,
    model: str | None = None,
    counter: Mapping[str, TokenCounter] | None = None,
    section_bounds: Mapping[int, int] | None = None,
    per_source_cap: int | None = None,
    per_parent_cap: int | None = None,
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
    if per_parent_cap is not None and (not _is_nonnegative_int(per_parent_cap)):
        raise ValueError("per_parent_cap must be a nonnegative integer")
    if emergency_byte_cap is not None and not _is_nonnegative_int(emergency_byte_cap):
        raise ValueError("emergency_byte_cap must be a nonnegative integer")

    normalized = _normalize_items(items)
    active_model = budget.model if model is None else model
    if model is not None and budget.model is not None and model != budget.model:
        raise ValueError("model must match budget.model")
    costs, item_sources = _item_costs(
        normalized, model=active_model, counter=counter
    )
    cost_by_id = {item.item_id: cost for item, cost in zip(normalized, costs)}
    source_by_id = {
        item.item_id: source for item, source in zip(normalized, item_sources)
    }

    dropped: list[DroppedItem] = []
    mandatory: list[ContextItem] = []
    optional: list[ContextItem] = []
    for item in normalized:
        if item.mandatory:
            mandatory.append(item)
        else:
            optional.append(item)

    mandatory = sorted(
        mandatory,
        key=lambda item: (PRIORITY_CLASS_ORDER[item.priority_class], item.item_id),
    )
    mandatory_text = _render(mandatory)
    mandatory_tokens, mandatory_render_source = _measure_rendered(
        mandatory_text, model=active_model, counter=counter
    )
    if mandatory_tokens > budget.available_input_tokens:
        raise BudgetExceededError(
            BudgetFailure(
                code="mandatory_budget_exceeded",
                mandatory_item_ids=tuple(item.item_id for item in mandatory),
                required_tokens=mandatory_tokens,
                available_tokens=budget.available_input_tokens,
                model=active_model,
                counter_source=mandatory_render_source,
            )
        )

    ranked = _ranked(optional, cost_by_id)

    # Caps are applied after utility ranking so lower-value input order cannot win.
    section_used: dict[int, int] = {}
    section_survivors: list[ContextItem] = []
    for item in ranked:
        cap = normalized_bounds.get(item.priority) if normalized_bounds else None
        if cap is not None:
            used = section_used.get(item.priority, 0)
            cost = cost_by_id[item.item_id]
            if used + cost > cap:
                dropped.append(DroppedItem(item.item_id, "section"))
                continue
            section_used[item.priority] = used + cost
        section_survivors.append(item)
    ranked = section_survivors

    source_counts: dict[str, int] = {}
    parent_counts: dict[str, int] = {}
    kept_optional: list[ContextItem] = []
    for item in ranked:
        if per_source_cap is not None and source_counts.get(item.source, 0) >= per_source_cap:
            dropped.append(DroppedItem(item.item_id, "diversity"))
            continue
        parent = item.parent_id or item.source
        if per_parent_cap is not None and parent_counts.get(parent, 0) >= per_parent_cap:
            dropped.append(DroppedItem(item.item_id, "diversity"))
            continue
        candidate = [*mandatory, *kept_optional, item]
        candidate_tokens, _ = _measure_rendered(
            _render(candidate), model=active_model, counter=counter
        )
        if candidate_tokens > budget.available_input_tokens:
            dropped.append(DroppedItem(item.item_id, "budget"))
            continue
        kept_optional.append(item)
        source_counts[item.source] = source_counts.get(item.source, 0) + 1
        parent_counts[parent] = parent_counts.get(parent, 0) + 1

    packed_items = [*mandatory, *kept_optional]

    text = _render(packed_items)
    truncated = False
    if emergency_byte_cap is not None and len(text.encode("utf-8")) > emergency_byte_cap:
        while kept_optional and len(text.encode("utf-8")) > emergency_byte_cap:
            kept_optional.pop()
            victim = packed_items.pop()
            text = _render(packed_items)
            truncated = True
            dropped.append(DroppedItem(victim.item_id, "emergency_cap"))
        if len(text.encode("utf-8")) > emergency_byte_cap:
            required, source = _measure_rendered(
                text, model=active_model, counter=counter
            )
            raise BudgetExceededError(
                BudgetFailure(
                    code="mandatory_emergency_cap_exceeded",
                    mandatory_item_ids=tuple(item.item_id for item in mandatory),
                    required_tokens=required,
                    available_tokens=budget.available_input_tokens,
                    model=active_model,
                    counter_source=source,
                    required_bytes=len(text.encode("utf-8")),
                    available_bytes=emergency_byte_cap,
                )
            )

    packed_tokens, rendered_source = _measure_rendered(
        text, model=active_model, counter=counter
    )
    counter_source = _combined_source(
        [rendered_source, *(source_by_id[item.item_id] for item in packed_items)]
    )
    return PackedContext(
        items=tuple(packed_items),
        text=text,
        packed_tokens=packed_tokens,
        counter_source=counter_source,
        dropped=tuple(dropped),
        budget=budget,
        truncated=truncated,
    )
