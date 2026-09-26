"""A dead-code claim stands only where the names were read (audit 2026-09-26 A-7).

docs/research/2026-09-26-a-dead-code-claim-names-what-it-checked.md
"""
from __future__ import annotations

import code_graph
import pytest
import value_references


def _row(path: str) -> dict:
    node = {"node_id": "code:function:" + "a" * 32, "metadata": {"name": "render", "owner": ""}}
    return code_graph._dead_candidate_row(node, (path, 3), frozenset(), value_references.EMPTY_INDEX)


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("src/app.py", "zero_confirmed_incoming_calls"),
        ("web/app.ts", "references_not_indexed"),
        ("cmd/main.go", "references_not_indexed"),
        ("src/lib.rs", "references_not_indexed"),
    ],
)
def test_the_strongest_verdict_is_kept_for_python_only(path: str, reason: str) -> None:
    assert _row(path)["reason"] == reason


def test_the_unchecked_verdict_is_cut_first() -> None:
    order = code_graph.DEAD_CODE_REASON_ORDER

    assert order.index("references_not_indexed") == len(order) - 1
