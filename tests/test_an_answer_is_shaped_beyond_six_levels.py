from __future__ import annotations

import answer_budget as budget
import pytest


def _wrapped(value, levels=14):
    for _ in range(levels):
        value = {"next": value}
    return value


def _leaf(value, levels=14):
    for _ in range(levels):
        value = value["next"]
    return value


def test_opaque_ids_at_deep_levels_are_removed_but_citations_are_preserved():
    row = {"node_id": "code:node:" + "a" * 32, "file": "repo/code.py", "line": 9}
    answer = budget.shape_code_answer(_wrapped(row))
    assert _leaf(answer) == {"file": "repo/code.py", "line": 9}
    assert answer["answer_budget"]["omitted_fields"] == ["node_id"]
    assert row["node_id"] == "code:node:" + "a" * 32


def test_repeated_module_names_are_shortened_at_every_depth():
    answer = budget.shape_code_answer(_wrapped({"file": "scripts/retrieval.py", "name": "scripts.retrieval.actual"}))
    assert _leaf(answer)["name"] == "actual"


def test_deep_row_tables_fit_the_requested_budget_without_losing_citation_fields():
    rows = [{"name": f"symbol{n}", "file": f"repo/code{n}.py", "line": n, "detail": f"distinct evidence {n} " * 12} for n in range(40)]
    answer = budget.shape_code_answer(_wrapped({"rows": rows}), budget_tokens=600)
    assert budget.estimate_tokens(answer) <= 600
    assert answer["answer_budget"]["truncated"] is True
    assert 0 < answer["answer_budget"]["rows_omitted"] < 40
    kept = _leaf(answer)
    decoded = _decoded_rows(kept)
    assert decoded
    assert all({"file", "line"}.issubset(row) for row in decoded)


def test_shared_acyclic_rows_are_valid_and_the_original_tree_stays_unchanged():
    row = {"node_id": "code:node:" + "a" * 32, "file": "repo/code.py", "line": 9}
    original = {"left": _wrapped(row), "right": _wrapped(row)}
    answer = budget.shape_code_answer(original)
    assert _leaf(answer["left"]) == _leaf(answer["right"]) == {"file": "repo/code.py", "line": 9}
    assert "node_id" in row


def test_very_deep_opaque_collections_do_not_require_python_recursion():
    value = "code:node:" + "a" * 32
    for _ in range(1500):
        value = [value]
    answer = budget.shape_code_answer({"communities": value, "file": "repo/code.py"})
    assert "communities" not in answer
    assert answer["file"] == "repo/code.py"


def test_cyclic_opaque_candidates_are_refused_as_invalid_data():
    value = []
    value.append(value)
    with pytest.raises(ValueError, match="[Cc]ircular|[Cc]yclic"):
        budget.shape_code_answer({"communities": value})


def _decoded_rows(value):
    columns = value.get("rows_cols")
    if columns:
        return [dict(zip(columns, row)) for row in value["rows"]]
    return value["rows"]


def test_recoverable_identity_is_removed_at_deep_levels():
    row = {"identity_key": "repository:" + "a" * 64 + "\x1fpython\x1fname\x1frepo/code.py", "metadata": {"name": "name", "path": "repo/code.py"}}
    answer = budget.shape_code_answer(_wrapped(row))
    assert "identity_key" not in _leaf(answer)
    assert _leaf(answer)["metadata"] == row["metadata"]


def test_deep_table_constants_and_columnar_shape_match_the_shallow_contract():
    value = {"rows": [{"file": f"repo/code{n}.py", "line": n, "status": "candidate", "graph_complete": False} for n in range(30)]}
    shallow = budget.shape_code_answer(value)
    deep = budget.shape_code_answer(_wrapped(value))
    assert _leaf(deep) == shallow
    assert "rows_row_constants" in shallow
