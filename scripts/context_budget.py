"""Token accounting contracts used before and after LLM calls."""
from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
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


def _is_positive_int(value: object) -> bool:
    return _is_nonnegative_int(value) and value != 0


def _nonnegative_finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    return math.isfinite(float(value)) and value >= 0


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _valid_count(value: object, optional: bool) -> bool:
    if optional and value is None:
        return True
    return _is_nonnegative_int(value)


def _require_counts(owner: object, names: tuple[str, ...], suffix: str, *, optional: bool = False) -> None:
    """Raise for the first named field that is not a nonnegative integer (or None, when optional)."""
    invalid = next((name for name in names if not _valid_count(getattr(owner, name), optional)), None)
    if invalid is not None:
        raise ValueError(f"{invalid}{suffix}")


def _require_fields(owner: object, checks: tuple) -> None:
    """Raise the message of the first (field, predicate, message) check that fails, in order."""
    for name, valid, message in checks:
        if not valid(getattr(owner, name)):
            raise ValueError(message)


@dataclass(frozen=True)
class ContextBudget:
    model: str | None
    max_input_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int

    def __post_init__(self) -> None:
        if not _is_positive_int(self.max_input_tokens):
            raise ValueError("max_input_tokens must be a positive integer")
        _require_counts(
            self, ("reserved_output_tokens", "safety_margin_tokens"), " must be a nonnegative integer"
        )
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
        _require_counts(
            self,
            ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "duration_ms"),
            " must be a nonnegative integer or None",
            optional=True,
        )
        if self.estimated_cost is not None and not _nonnegative_finite(self.estimated_cost):
            raise ValueError("estimated_cost must be a nonnegative finite number or None")
        self._require_cost_kind()

    def _require_cost_kind(self) -> None:
        if self.cost_kind not in {"reported", "estimated", "unknown"}:
            raise ValueError("cost_kind is invalid")
        if (self.estimated_cost is None) != (self.cost_kind == "unknown"):
            raise ValueError("cost_kind must describe an existing monetary cost")


@dataclass(frozen=True)
class TokenCount:
    tokens: int | None = None
    source: CountSource = "unknown"

    def __post_init__(self) -> None:
        if self.source not in {"reported", "tokenizer", "estimated", "mixed", "unknown"}:
            raise ValueError("source is invalid")
        self._require_consistent_tokens()

    def _require_consistent_tokens(self) -> None:
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
    return _counted(text, _adapter_for(adapters, model))


def _counted(text: str, adapter: TokenCounter | None) -> TokenCount:
    if adapter is None:
        return _byte_estimate(text)
    return _adapter_count(adapter, text)


def _adapter_for(
    adapters: Mapping[str, TokenCounter] | None, model: str | None
) -> TokenCounter | None:
    if adapters is None or model is None:
        return None
    return adapters.get(model)


def _adapter_count(adapter: TokenCounter, text: str) -> TokenCount:
    try:
        count = adapter(text)
    except Exception:  # noqa: BLE001 - adapters are an optional isolation boundary
        return TokenCount()
    if not _is_nonnegative_int(count):
        return TokenCount()
    return TokenCount(count, "tokenizer")


def _byte_estimate(text: str) -> TokenCount:
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        return TokenCount()
    return TokenCount(len(encoded), "estimated")


def fits_within_budget(count: TokenCount, budget: ContextBudget) -> bool:
    """Conservatively accept only known counts within the budget's safe input."""
    return count.tokens is not None and count.tokens <= budget.available_input_tokens


_ITEM_CHECKS = (
    ("item_id", _nonempty_text, "item_id must be a non-empty string"),
    ("text", lambda value: isinstance(value, str), "text must be a string"),
    ("source", _nonempty_text, "source must be a non-empty string"),
    ("priority", _is_nonnegative_int, "priority must be a nonnegative integer"),
    ("relevance", _nonnegative_finite, "relevance must be a nonnegative finite number"),
    ("confidence", _nonempty_text, "confidence must be a non-empty string"),
    ("freshness", _nonempty_text, "freshness must be a non-empty string"),
    ("token_cost", _is_nonnegative_int, "token_cost must be a nonnegative integer"),
    ("mandatory", lambda value: isinstance(value, bool), "mandatory must be a boolean"),
    ("representation", _nonempty_text, "representation must be a non-empty string"),
    (
        "parent_id",
        lambda value: value is None or _nonempty_text(value),
        "parent_id must be a non-empty string or None",
    ),
    ("priority_class", lambda value: value in PRIORITY_CLASS_ORDER, "priority_class is invalid"),
)


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
        _require_fields(self, _ITEM_CHECKS)


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
    ranked_item_ids: tuple[str, ...]


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

    def render(self, *, max_bytes: int = 1024) -> str:
        if not _is_positive_int(max_bytes):
            raise ValueError("max_bytes must be a positive integer")
        for candidate in (self._markdown(), self._compact(), self._minimal(), '{"error":"budget"}'):
            if len(candidate.encode("utf-8")) <= max_bytes:
                return candidate
        raise ValueError("max_bytes is too small for a structured budget diagnostic")

    def _markdown(self) -> str:
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
        return rendered + self._byte_lines()

    def _byte_lines(self) -> str:
        if self.required_bytes is None or self.available_bytes is None:
            return ""
        return (
            f"\n- required_bytes: `{self.required_bytes}`"
            f"\n- available_bytes: `{self.available_bytes}`"
        )

    def _compact(self) -> str:
        return json.dumps(
            {
                "error": self.code,
                "mandatory_count": len(self.mandatory_item_ids),
                "required_tokens": self.required_tokens,
                "available_tokens": self.available_tokens,
                "model": self.model,
                "counter_source": self.counter_source,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _minimal(self) -> str:
        return json.dumps(
            {"error": self.code, "mandatory_count": len(self.mandatory_item_ids)},
            separators=(",", ":"),
        )


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
    adapter = _adapter_for(counter, model)
    measured = [_item_cost(adapter, item) for item in items]
    return tuple(cost for cost, _ in measured), tuple(source for _, source in measured)


def _item_cost(adapter: TokenCounter | None, item: ContextItem) -> tuple[int, CountSource]:
    if adapter is None:
        return item.token_cost, "estimated"
    return _measured_item_cost(adapter, item)


def _measured_item_cost(adapter: TokenCounter, item: ContextItem) -> tuple[int, CountSource]:
    try:
        measured = adapter(item.text)
    except Exception:  # noqa: BLE001 - adapters are an optional isolation boundary
        return item.token_cost, "estimated"
    if not _is_nonnegative_int(measured):
        return item.token_cost, "estimated"
    return measured, "tokenizer"


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
    normalized_bounds = _normalized_bounds(section_bounds)
    _require_caps(per_source_cap, per_parent_cap, emergency_byte_cap)
    normalized = _normalize_items(items)
    active_model = _active_model(budget, model)
    packer = _Packer(_budget_for(budget, active_model), active_model, counter, (per_source_cap, per_parent_cap))
    cost_by_id, source_by_id = _cost_maps(normalized, active_model, counter)
    mandatory, optional = _partition(normalized)
    packer.reserve(mandatory, budget.available_input_tokens)
    ranked = _ranked(optional, cost_by_id)
    ranked_item_ids = packer.ranked_ids(ranked)
    # Caps are applied after utility ranking so lower-value input order cannot win.
    packer.fill(_within_sections(ranked, normalized_bounds, cost_by_id, packer.dropped))
    truncated = packer.fit_byte_cap(emergency_byte_cap)
    return packer.result(source_by_id, truncated, ranked_item_ids)


def _normalized_bounds(section_bounds: Mapping[int, int] | None) -> dict[int, int]:
    if section_bounds is None:
        return {}
    return {int(key): (0 if value is None else int(value)) for key, value in section_bounds.items()}


_CAP_NAMES = ("per_source_cap", "per_parent_cap", "emergency_byte_cap")


def _require_caps(*caps: int | None) -> None:
    for name, cap in zip(_CAP_NAMES, caps):
        if cap is not None and not _is_nonnegative_int(cap):
            raise ValueError(f"{name} must be a nonnegative integer")


def _active_model(budget: ContextBudget, model: str | None) -> str | None:
    if model is None:
        return budget.model
    if budget.model is not None and model != budget.model:
        raise ValueError("model must match budget.model")
    return model


def _budget_for(budget: ContextBudget, model: str | None) -> ContextBudget:
    if budget.model == model:
        return budget
    return replace(budget, model=model)


def _cost_maps(
    normalized: tuple[ContextItem, ...], model: str | None, counter: Mapping[str, TokenCounter] | None
) -> tuple[dict[str, int], dict[str, CountSource]]:
    costs, sources = _item_costs(normalized, model=model, counter=counter)
    ids = [item.item_id for item in normalized]
    return dict(zip(ids, costs)), dict(zip(ids, sources))


def _partition(normalized: tuple[ContextItem, ...]) -> tuple[list[ContextItem], list[ContextItem]]:
    """(mandatory items in priority-class order, optional items in input order)."""
    mandatory = sorted(
        (item for item in normalized if item.mandatory),
        key=lambda item: (PRIORITY_CLASS_ORDER[item.priority_class], item.item_id),
    )
    optional = [item for item in normalized if not item.mandatory]
    return mandatory, optional


def _within_sections(
    ranked: list[ContextItem], bounds: dict[int, int], cost_by_id: Mapping[str, int], dropped: list
) -> list[ContextItem]:
    used: dict[int, int] = {}
    survivors: list[ContextItem] = []
    for item in ranked:
        if not _section_admits(item, bounds, used, cost_by_id[item.item_id]):
            dropped.append(DroppedItem(item.item_id, "section"))
            continue
        survivors.append(item)
    return survivors


def _section_admits(item: ContextItem, bounds: dict[int, int], used: dict[int, int], cost: int) -> bool:
    cap = bounds.get(item.priority)
    if cap is None:
        return True
    total = used.get(item.priority, 0) + cost
    if total > cap:
        return False
    used[item.priority] = total
    return True


def _at_cap(counts: dict[str, int], key: str, cap: int | None) -> bool:
    return cap is not None and counts.get(key, 0) >= cap


def _parent_key(item: ContextItem) -> str:
    return item.parent_id or item.source


class _Packer:
    """One packing pass: the mandatory core, the optional items kept, and every drop."""

    def __init__(
        self,
        budget: ContextBudget,
        model: str | None,
        counter: Mapping[str, TokenCounter] | None,
        caps: tuple[int | None, int | None],
    ) -> None:
        self.budget = budget
        self.model = model
        self.counter = counter
        self.per_source_cap, self.per_parent_cap = caps
        self.mandatory: list[ContextItem] = []
        self.kept: list[ContextItem] = []
        self.dropped: list[DroppedItem] = []
        self.source_counts: dict[str, int] = {}
        self.parent_counts: dict[str, int] = {}

    def measure(self, text: str) -> tuple[int, CountSource]:
        return _measure_rendered(text, model=self.model, counter=self.counter)

    def packed(self) -> list[ContextItem]:
        return [*self.mandatory, *self.kept]

    def reserve(self, mandatory: list[ContextItem], available_tokens: int) -> None:
        self.mandatory = mandatory
        tokens, source = self.measure(_render(mandatory))
        if tokens > available_tokens:
            raise BudgetExceededError(self._failure("mandatory_budget_exceeded", tokens, source))

    def ranked_ids(self, ranked: list[ContextItem]) -> tuple[str, ...]:
        return tuple(item.item_id for item in [*self.mandatory, *ranked])

    def fill(self, candidates: list[ContextItem]) -> None:
        for item in candidates:
            self._admit(item)

    def _admit(self, item: ContextItem) -> None:
        reason = self._rejection(item)
        if reason is not None:
            self.dropped.append(DroppedItem(item.item_id, reason))
            return
        self.kept.append(item)
        self.source_counts[item.source] = self.source_counts.get(item.source, 0) + 1
        parent = _parent_key(item)
        self.parent_counts[parent] = self.parent_counts.get(parent, 0) + 1

    def _rejection(self, item: ContextItem) -> str | None:
        if self._diversity_exhausted(item):
            return "diversity"
        candidate_tokens, _ = self.measure(_render([*self.mandatory, *self.kept, item]))
        if candidate_tokens > self.budget.available_input_tokens:
            return "budget"
        return None

    def _diversity_exhausted(self, item: ContextItem) -> bool:
        return _at_cap(self.source_counts, item.source, self.per_source_cap) or _at_cap(
            self.parent_counts, _parent_key(item), self.per_parent_cap
        )

    def fit_byte_cap(self, cap: int | None) -> bool:
        """Drop the least important optional items until the text fits; whether any went."""
        if cap is None:
            return False
        truncated = self._drop_until_fits(cap)
        text = _render(self.packed())
        if len(text.encode("utf-8")) > cap:
            required, source = self.measure(text)
            raise BudgetExceededError(
                self._failure(
                    "mandatory_emergency_cap_exceeded",
                    required,
                    source,
                    required_bytes=len(text.encode("utf-8")),
                    available_bytes=cap,
                )
            )
        return truncated

    def _drop_until_fits(self, cap: int) -> bool:
        truncated = False
        while self.kept and len(_render(self.packed()).encode("utf-8")) > cap:
            victim = self.kept.pop()
            self.dropped.append(DroppedItem(victim.item_id, "emergency_cap"))
            truncated = True
        return truncated

    def _failure(self, code: str, required_tokens: int, source: CountSource, **byte_bounds: int) -> BudgetFailure:
        return BudgetFailure(
            code=code,
            mandatory_item_ids=tuple(item.item_id for item in self.mandatory),
            required_tokens=required_tokens,
            available_tokens=self.budget.available_input_tokens,
            model=self.model,
            counter_source=source,
            **byte_bounds,
        )

    def result(
        self, source_by_id: Mapping[str, CountSource], truncated: bool, ranked_item_ids: tuple[str, ...]
    ) -> PackedContext:
        packed_items = self.packed()
        text = _render(packed_items)
        packed_tokens, rendered_source = self.measure(text)
        counter_source = _combined_source(
            [rendered_source, *(source_by_id[item.item_id] for item in packed_items)]
        )
        return PackedContext(
            items=tuple(packed_items),
            text=text,
            packed_tokens=packed_tokens,
            counter_source=counter_source,
            dropped=tuple(self.dropped),
            budget=self.budget,
            truncated=truncated,
            ranked_item_ids=ranked_item_ids,
        )
