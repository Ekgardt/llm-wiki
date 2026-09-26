"""A context answer sends its text once and keeps each class in the caller's order (audit 2026-09-26 B-18).

docs/research/2026-09-26-a-context-answer-sends-its-text-once.md
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import context_budget
import mcp_server
from context_compiler import CompiledItem


def _item(item_id: str, priority_class: str = "evidence") -> context_budget.ContextItem:
    return context_budget.ContextItem(
        item_id=item_id, text=f"text of {item_id}", source="knowledge/notes/a.md", priority=1, relevance=1.0,
        confidence="high", freshness="fresh", token_cost=1, mandatory=True, representation="l2",
        priority_class=priority_class,
    )


def test_mandatory_items_keep_the_callers_order_within_a_class() -> None:
    items = [_item("f" * 8), _item("0" * 8), _item("9" * 8)]

    assert [item.item_id for item in context_budget._in_class_order(items)] == ["f" * 8, "0" * 8, "9" * 8]


@dataclass
class _Empty:
    pass


def test_the_lists_name_the_items_without_repeating_their_text() -> None:
    trace = SimpleNamespace(retrieval=_Empty(), materializations=(), packing=_Empty())
    page = CompiledItem(
        item_id="a" * 8, text="text of " + "a" * 8, source="knowledge/notes/a.md", parent_id="p", representation="l2",
        heading_path=(), byte_start=0, byte_end=1, source_sha256="0" * 64, project=None, type="concept",
        status=None, valid_from=None, valid_to=None, aliases=(), relevance=1.0,
    )
    compiled = SimpleNamespace(items=(page,), text="text of " + "a" * 8, packed_tokens=1, trace=trace)
    snapshot = SimpleNamespace(corpus_sha256="0" * 64)
    selection = {"selected_paths": set(), "missing": []}

    result = mcp_server._context_result(compiled, snapshot, selection, 100, None)

    assert ("text" in result["pages"][0], result["text"]) == (False, "text of " + "a" * 8)
