"""A dead-code claim stands only where the names were read (audit 2026-09-26 A-7).

docs/research/2026-09-26-a-dead-code-claim-names-what-it-checked.md
"""
from __future__ import annotations

import code_graph
import pytest
import value_references


def _row(path: str, index=value_references.EMPTY_INDEX) -> dict:
    node = {"node_id": "code:function:" + "a" * 32, "metadata": {"name": "render", "owner": ""}}
    return code_graph.dead_candidate_row(node, (path, 3), frozenset(), index)


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


@pytest.mark.parametrize('source', [b'lookup="\\u0072ender"', b'lookup="ren" "der"'])
def test_transformed_reference_cannot_receive_the_unused_verdict(source):
    index = value_references.build_reference_index([('caller.py', source)], names=frozenset({'render'}))
    assert _row('src/app.py', index)['reason'] == 'referenced_without_call'
