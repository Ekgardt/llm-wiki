"""A short path prefix is allowed to pay for its own representation."""
import answer_budget
import pytest


@pytest.mark.parametrize("prefix", ["/a/", "/ж/"])
def test_short_prefix_is_used_when_its_complete_shape_is_cheaper(prefix):
    rows = [{"path": f"{prefix}{index}"} for index in range(40)]
    result = answer_budget._prefixes_if_cheaper("rows", rows)
    assert result["rows_row_prefixes"] == {"path": prefix}
    restored = [{"path": prefix + row["path"]} for row in result["rows"]]
    assert restored == rows
    assert answer_budget.estimate_tokens(result) < answer_budget.estimate_tokens({"rows": rows})


def test_an_unprofitable_prefix_leaves_the_rows_unchanged():
    rows = [{"path": "/a/1"}, {"path": "/a/2"}]
    before = [dict(row) for row in rows]
    assert answer_budget._prefixes_if_cheaper("rows", rows) == {}
    assert rows == before
