"""The lifecycle vocabulary, separate from corpus integration."""
import pytest
from page_status import current_status_sql, is_retired


@pytest.mark.parametrize(
    ("status", "retired"),
    [
        ("active", False),
        ("accepted", False),
        ("preliminary", False),
        ("proposed", False),
        ("", False),
        (None, False),
        ("ACCEPTED", False),
        ("superseded", True),
        ("archived", True),
        ("deprecated", True),
        ("rejected", True),
        ("`superseded`", True),
    ],
)
def test_only_the_named_words_retire_a_page(status: object, retired: bool) -> None:
    assert is_retired(status) is retired


def test_the_sql_rule_is_generated_from_the_same_set() -> None:
    clause = current_status_sql()
    assert "'superseded'" in clause and "'deprecated'" in clause
    assert "'accepted'" not in clause
    assert "NOT IN" in clause

