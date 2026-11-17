"""Original pure answer-shaping assertions, independent from MCP."""
from __future__ import annotations

import answer_budget
import pytest

from tests.modules.answer_budget.helpers import _table


def test_the_module_refuses_a_budget_it_does_not_serve():
    with pytest.raises(ValueError, match="budget_tokens"):
        answer_budget.shape_code_answer({"a": 1}, budget_tokens=0)


def test_a_boolean_is_not_a_budget():
    with pytest.raises(ValueError, match="budget_tokens"):
        answer_budget.shape_code_answer({"a": 1}, budget_tokens=True)


def test_an_answer_with_nothing_to_drop_is_returned_unchanged():
    answer = {"status": "error", "mode": "summary", "error": "row ceiling exceeded"}
    assert answer_budget.shape_code_answer(answer) == answer


def test_a_readable_group_is_not_an_opaque_collection():
    """Only the `code:<kind>:<hash>` form counts; names are not hashes."""
    answer = {"architecture": {"communities": [["caller", "callee"]]}}
    assert answer_budget.shape_code_answer(answer) == answer


_REPO_DIGEST = "d8c142988412f9d857dbb61c5bd0ee14ddd9aa22ef34071cf585e847ed76a484"


def _module_row(name: str, path: str, key: str | None = None) -> dict:
    default = f"repository:{_REPO_DIGEST}\x1fpython\x1f{name}\x1f{path}"
    return {
        "identity_key": default if key is None else key,
        "metadata": {"name": name, "path": path},
        "depth": 1,
    }


def _dependencies(rows: list) -> dict:
    return {"mode": "dependencies", "architecture": {"dependencies": rows}}


def test_an_identity_key_the_row_already_states_in_the_open_goes():
    """The second mint: `repository:<64 hex>` ended in readable text and slipped.

    Measured 2026-08-29 on this repository: `mode=dependencies` answered in
    23 849 tokens, of which `identity_key` was 14 564 (61.1%) and the constant
    `repository:<64 hex>` prefix alone 6 112, repeated on all 326 rows -- while
    `metadata` carried the same name and path in the open.
    """
    rows = [_module_row("scripts.page_status", "scripts/page_status.py")]
    shaped = answer_budget.shape_code_answer(_dependencies(rows))
    kept = shaped["architecture"]["dependencies"][0]
    assert "identity_key" not in kept
    assert kept["metadata"] == {
        "name": "scripts.page_status",
        "path": "scripts/page_status.py",
    }
    assert shaped["answer_budget"]["omitted_fields"] == ["identity_key"]


def test_an_identity_key_holding_something_the_row_does_not_say_stays():
    """The rule may never be the reason a fact left the answer."""
    unstated = f"repository:{_REPO_DIGEST}\x1fpython\x1fscripts.other\x1fscripts/other.py"
    rows = [_module_row("scripts.page_status", "scripts/page_status.py", key=unstated)]
    shaped = answer_budget.shape_code_answer(_dependencies(rows))
    assert shaped["architecture"]["dependencies"][0]["identity_key"] == unstated


def test_a_column_that_never_varies_is_stated_once_not_once_per_row():
    """Lossless: the value is still in the answer, exactly once."""
    rows = [
        {"name": "a", "file": "x.py", "status": "candidate", "graph_complete": False},
        {"name": "b", "file": "y.py", "status": "candidate", "graph_complete": False},
        {"name": "c", "file": "z.py", "status": "candidate", "graph_complete": False},
    ]
    shaped = answer_budget.shape_code_answer({"candidates": list(rows)})
    assert shaped["candidates_row_constants"] == {
        "status": "candidate",
        "graph_complete": False,
    }
    assert _table(shaped, "candidates") == [
        {"name": "a", "file": "x.py"},
        {"name": "b", "file": "y.py"},
        {"name": "c", "file": "z.py"},
    ]
    assert answer_budget.estimate_tokens(shaped) < answer_budget.estimate_tokens(
        {"candidates": rows}
    )


def test_a_short_table_whose_constants_cost_more_than_they_save_does_not_move():
    """Decided by measuring both shapes, not by a threshold on row count."""
    answer = {"rows": [{"a": 1, "b": 2}, {"a": 1, "b": 3}]}
    assert answer_budget.shape_code_answer(answer) == answer


def test_a_column_mixing_false_and_zero_is_not_called_constant():
    """Type-strict, because `False == 0` in Python and they are not one value."""
    rows = [
        {"name": "a" * 40, "flag": False},
        {"name": "b" * 40, "flag": 0},
        {"name": "c" * 40, "flag": False},
    ]
    shaped = answer_budget.shape_code_answer({"rows": list(rows)})
    assert "rows_row_constants" not in shaped
    assert _table(shaped, "rows") == rows


def test_a_long_table_states_its_header_once_and_loses_nothing():
    """The columnar move of 2026-09-12: cheaper, and recoverable row for row."""
    rows = [
        {"qualified_name": f"scripts.retrieval.caller_{index}", "line": 1000 + index}
        for index in range(12)
    ]
    shaped = answer_budget.shape_code_answer({"callers": list(rows)})

    assert shaped["callers_cols"] == ["line", "qualified_name"]
    assert _table(shaped, "callers") == rows
    assert answer_budget.estimate_tokens(shaped) < answer_budget.estimate_tokens(
        {"callers": rows}
    )


def test_one_shared_path_prefix_is_stated_once_not_once_per_row():
    """Lossless: the prefix is still in the answer, exactly once."""
    rows = [
        {"file": f"/repo/llm-wiki-tasks/benchmark/module_{index}.py", "line": index}
        for index in range(12)
    ]
    shaped = answer_budget.shape_code_answer({"entry_points": list(rows)})

    assert shaped["entry_points_row_prefixes"] == {
        "file": "/repo/llm-wiki-tasks/benchmark/"
    }
    restored = [
        {**row, "file": shaped["entry_points_row_prefixes"]["file"] + row["file"]}
        for row in _table(shaped, "entry_points")
    ]
    assert restored == rows
    assert answer_budget.estimate_tokens(shaped) < answer_budget.estimate_tokens(
        {"entry_points": rows}
    )
