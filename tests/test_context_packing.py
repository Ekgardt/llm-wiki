"""Task 14: complete-item token packing under a shared ContextBudget.

The packer must:
- reserve mandatory items whole, and fail visibly when they cannot fit,
- enforce per-section token bounds (priority classes),
- pick optional items deterministically by utility per token,
- apply per-source diversity caps,
- reject impossible budgets explicitly,
- account for Unicode and code token costs honestly,
- never slice Markdown mid-item,
- report packed token count, counter source, and dropped item IDs/reasons.
"""
from __future__ import annotations

import json

import pytest
from context_budget import (
    BudgetExceededError,
    ContextBudget,
    ContextItem,
    DroppedItem,
    PackedContext,
    pack_context,
)


def _item(
    item_id: str,
    text: str,
    *,
    source: str = "knowledge/notes/example.md",
    priority: int = 5,
    relevance: float = 0.5,
    confidence: str = "medium",
    freshness: str = "fresh",
    mandatory: bool = False,
    representation: str = "l1",
    parent_id: str | None = None,
    priority_class: str = "evidence",
) -> ContextItem:
    return ContextItem(
        item_id=item_id,
        text=text,
        source=source,
        priority=priority,
        relevance=relevance,
        confidence=confidence,
        freshness=freshness,
        token_cost=len(text.encode("utf-8")),
        mandatory=mandatory,
        representation=representation,
        parent_id=parent_id,
        priority_class=priority_class,
    )


def test_context_item_is_frozen_and_validates_token_cost():
    item = _item("a", "x")

    with pytest.raises(AttributeError):
        item.token_cost = 0  # type: ignore[misc]

    with pytest.raises(ValueError, match="token_cost"):
        ContextItem(
            item_id="x",
            text="x",
            source="s",
            priority=1,
            relevance=0.0,
            confidence="low",
            freshness="unknown",
            token_cost=-1,
            mandatory=False,
            representation="l0",
        )


def test_mandatory_items_reserved_first_and_fit():
    budget = ContextBudget(None, max_input_tokens=200, reserved_output_tokens=0, safety_margin_tokens=0)
    mandatory = _item("safety", "Do not commit without permission.", mandatory=True, priority=1)
    optional = _item("history", "yesterday we tried x" * 20, priority=7, relevance=0.1)

    packed = pack_context([optional, mandatory], budget)

    assert [i.item_id for i in packed.items] == ["safety"]
    assert packed.packed_tokens == mandatory.token_cost
    assert packed.counter_source == "estimated"
    assert packed.truncated is False
    assert packed.dropped == (DroppedItem("history", "budget"),)


def test_impossible_mandatory_budget_raises_structured_visibility():
    budget = ContextBudget(None, max_input_tokens=10, reserved_output_tokens=0, safety_margin_tokens=0)
    mandatory = _item("big", "x" * 200, mandatory=True, priority=1)

    with pytest.raises(BudgetExceededError, match="mandatory") as raised:
        pack_context([mandatory], budget)

    assert raised.value.failure.code == "mandatory_budget_exceeded"
    assert raised.value.failure.mandatory_item_ids == ("big",)
    assert raised.value.failure.required_tokens > raised.value.failure.available_tokens


def test_per_section_bounds_drop_low_priority_even_when_budget_remains():
    budget = ContextBudget(None, max_input_tokens=10_000, reserved_output_tokens=0, safety_margin_tokens=0)
    safety = _item("safety", "rule", mandatory=True, priority=1)
    overflow_history = [
        _item(f"h{i}", f"history line {i}", priority=7, relevance=0.1) for i in range(20)
    ]

    packed = pack_context(
        [safety, *overflow_history],
        budget,
        section_bounds={7: 20},  # only one short history item fits
    )

    history_kept = [i for i in packed.items if i.priority == 7]
    assert len(history_kept) == 1
    dropped = {d.item_id for d in packed.dropped}
    assert len(dropped) == 19


def test_deterministic_selection_by_utility_per_token_with_ties():
    budget = ContextBudget(None, max_input_tokens=100, reserved_output_tokens=0, safety_margin_tokens=0)
    # equal relevance, equal cost, equal priority — selection is deterministic
    # by item_id ascending so reruns are stable.
    items = [
        _item("c", "x", priority=5, relevance=0.5),
        _item("a", "x", priority=5, relevance=0.5),
        _item("b", "x", priority=5, relevance=0.5),
    ]

    packed = pack_context(items, budget)

    assert [i.item_id for i in packed.items] == ["a", "b", "c"]


def test_utility_ranking_happens_before_section_cap():
    budget = ContextBudget(None, 100, 0, 0)
    lower = _item("a-lower", "xxxx", priority=5, relevance=0.1)
    higher = _item("z-higher", "xxxx", priority=5, relevance=0.9)

    packed = pack_context([lower, higher], budget, section_bounds={5: 4})

    assert [item.item_id for item in packed.items] == ["z-higher"]
    assert packed.dropped == (DroppedItem("a-lower", "section"),)


def test_higher_relevance_per_token_wins_under_tight_budget():
    budget = ContextBudget(None, max_input_tokens=10, reserved_output_tokens=0, safety_margin_tokens=0)
    cheap_low_rel = _item("cheap", "abcdef", priority=5, relevance=0.1)  # 6 tokens, 0.0167/token
    pricey_high_rel = _item("pricey", "abcdefghij", priority=5, relevance=0.9)  # 10 tokens, 0.09/token

    packed = pack_context([cheap_low_rel, pricey_high_rel], budget)

    kept = {i.item_id for i in packed.items}
    assert kept == {"pricey"}
    assert any(d.item_id == "cheap" for d in packed.dropped)


def test_per_source_diversity_caps_overflowing_source():
    budget = ContextBudget(None, max_input_tokens=10_000, reserved_output_tokens=0, safety_margin_tokens=0)
    same_source = [
        _item(f"a{i}", f"line {i}", source="knowledge/notes/a.md", priority=5, relevance=0.5)
        for i in range(10)
    ]
    other_source = [
        _item(f"b{i}", f"line {i}", source="knowledge/notes/b.md", priority=5, relevance=0.5)
        for i in range(10)
    ]

    packed = pack_context([*same_source, *other_source], budget, per_source_cap=2)

    per_source = {}
    for item in packed.items:
        per_source[item.source] = per_source.get(item.source, 0) + 1
    assert per_source == {"knowledge/notes/a.md": 2, "knowledge/notes/b.md": 2}
    assert len(packed.dropped) == 16


def test_per_parent_diversity_caps_distinct_sources_for_same_parent():
    budget = ContextBudget(None, 100, 0, 0)
    items = [
        _item("low", "x", source="chunk-a", parent_id="page.md", relevance=0.1),
        _item("high", "x", source="chunk-b", parent_id="page.md", relevance=0.9),
        _item("other", "x", source="chunk-c", parent_id="other.md", relevance=0.5),
    ]

    packed = pack_context(items, budget, per_parent_cap=1)

    assert [item.item_id for item in packed.items] == ["high", "other"]
    assert DroppedItem("low", "diversity") in packed.dropped


def test_unicode_token_costs_counted_honestly():
    budget = ContextBudget(None, max_input_tokens=10, reserved_output_tokens=0, safety_margin_tokens=0)
    # 4 chars, 10 UTF-8 bytes (€ = 3, 😀 = 4, plus 2 ASCII) → conservatively
    # counts as 10 estimated tokens, not 4.
    unicode_item = _item("u", "A€😀", priority=1, relevance=1.0, mandatory=True)

    packed = pack_context([unicode_item], budget)

    assert packed.packed_tokens == len("A€😀".encode())
    assert packed.counter_source == "estimated"


def test_registered_tokenizer_counts_overrule_byte_estimate():
    budget = ContextBudget("my-model", max_input_tokens=10, reserved_output_tokens=0, safety_margin_tokens=0)
    item = _item("t", "abcdefghij" * 5, priority=1, relevance=1.0, mandatory=True)  # 50 bytes

    # tokenizer reports 4 tokens instead of 50 bytes — packing must use 4.
    packed = pack_context(
        [item],
        budget,
        counter={"my-model": lambda text: 4},
    )

    assert packed.packed_tokens == 4
    assert packed.counter_source == "tokenizer"


def test_model_argument_becomes_effective_budget_provenance():
    budget = ContextBudget(None, 10, 0, 0)
    packed = pack_context(
        [_item("item", "many bytes", mandatory=True)],
        budget,
        model="tokenizer-model",
        counter={"tokenizer-model": lambda _text: 2},
    )

    assert packed.budget.model == "tokenizer-model"
    assert packed.packed_tokens == 2


def test_final_rendered_separator_is_tokenized_and_budgeted():
    budget = ContextBudget("words", 3, 0, 0)
    items = [_item("a", "alpha"), _item("b", "beta")]

    packed = pack_context(
        items,
        budget,
        counter={"words": lambda text: len(text.replace("\n\n", " SEP ").split())},
    )

    assert packed.text == "alpha\n\nbeta"
    assert packed.packed_tokens == 3


def test_counter_source_is_mixed_when_tokenizer_falls_back_for_an_item():
    budget = ContextBudget("model", 100, 0, 0)
    items = [_item("a", "alpha"), _item("b", "fallback")]

    def count(text: str) -> int:
        if text == "fallback":
            raise ValueError("cannot encode")
        return 1

    packed = pack_context(items, budget, counter={"model": count})

    assert packed.counter_source == "mixed"
    assert packed.packed_tokens == 1


def test_no_mid_item_truncation_when_emergency_byte_cap_hits():
    # Budget large enough in tokens, but emergency byte cap tight enough to
    # force a drop. The dropped item must be whole — its text must not appear
    # truncated in the output.
    budget = ContextBudget(None, max_input_tokens=10_000, reserved_output_tokens=0, safety_margin_tokens=0)
    item_a = _item("a", "first paragraph end", priority=1, relevance=0.9)
    item_b = _item("b", "second paragraph end", priority=2, relevance=0.8)

    packed = pack_context(
        [item_a, item_b],
        budget,
        emergency_byte_cap=len(item_a.text),
    )

    assert packed.truncated is True
    assert "second paragraph end" not in packed.text
    assert "first paragraph end" in packed.text
    assert any(d.item_id == "b" and d.reason == "emergency_cap" for d in packed.dropped)


def test_emergency_cap_never_drops_or_slices_mandatory_item():
    budget = ContextBudget(None, 10_000, 0, 0)
    mandatory = _item("safety", "mandatory safety text", mandatory=True)

    with pytest.raises(BudgetExceededError) as raised:
        pack_context([mandatory], budget, emergency_byte_cap=5)

    assert raised.value.failure.code == "mandatory_emergency_cap_exceeded"
    assert raised.value.failure.mandatory_item_ids == ("safety",)


def test_impossible_budget_diagnostic_is_complete_and_bounded():
    budget = ContextBudget(None, 10, 0, 0)
    mandatory = _item("mandatory-" + "x" * 1000, "content" * 100, mandatory=True)

    with pytest.raises(BudgetExceededError) as raised:
        pack_context([mandatory], budget)

    rendered = raised.value.failure.render(max_bytes=160)
    payload = json.loads(rendered)
    assert len(rendered.encode("utf-8")) <= 160
    assert payload["error"] == "mandatory_budget_exceeded"
    assert payload["mandatory_count"] == 1


def test_packed_text_joins_complete_items_with_separators():
    budget = ContextBudget(None, max_input_tokens=10_000, reserved_output_tokens=0, safety_margin_tokens=0)
    items = [
        _item("a", "alpha", priority=1),
        _item("b", "beta", priority=2),
    ]

    packed = pack_context(items, budget)

    assert "alpha" in packed.text
    assert "beta" in packed.text
    # complete items, separated by a blank line
    assert packed.text.count("\n\n") >= 1


def test_empty_input_returns_empty_packed_context():
    budget = ContextBudget(None, max_input_tokens=100, reserved_output_tokens=0, safety_margin_tokens=0)

    packed = pack_context([], budget)

    assert isinstance(packed, PackedContext)
    assert packed.items == ()
    assert packed.packed_tokens == 0
    assert packed.dropped == ()
    assert packed.truncated is False


def test_unknown_counter_for_unknown_model_falls_back_to_byte_estimate():
    budget = ContextBudget(None, max_input_tokens=200, reserved_output_tokens=0, safety_margin_tokens=0)
    item = _item("u", "hello world", priority=1, relevance=1.0, mandatory=True)

    packed = pack_context([item], budget, model="no-such-adapter")

    assert packed.packed_tokens == len(b"hello world")
    assert packed.counter_source == "estimated"


def test_dropped_reasons_distinguish_budget_from_section_and_diversity():
    budget = ContextBudget(None, max_input_tokens=10_000, reserved_output_tokens=0, safety_margin_tokens=0)
    items = [
        _item("keep", "x", source="a.md", priority=5, relevance=0.9),
        _item("lower_rel", "x", source="a.md", priority=5, relevance=0.1),
        _item("over_section", "x", source="b.md", priority=7, relevance=0.1),
        _item("fits", "x", source="c.md", priority=3, relevance=0.8),
    ]

    packed = pack_context(
        items,
        budget,
        per_source_cap=1,
        section_bounds={7: 0},
    )

    reasons = {d.item_id: d.reason for d in packed.dropped}
    assert reasons["lower_rel"] == "diversity"
    assert reasons["over_section"] == "section"
    assert "fits" not in reasons
    assert "keep" not in reasons


def test_semantic_priority_classes_override_numeric_input_priority():
    budget = ContextBudget(None, 1, 0, 0)
    history = _item("history", "x", priority=1, priority_class="history", relevance=1.0)
    blocker = _item("blocker", "x", priority=99, priority_class="blocker", relevance=1.0)

    packed = pack_context([history, blocker], budget)

    assert [item.item_id for item in packed.items] == ["blocker"]
