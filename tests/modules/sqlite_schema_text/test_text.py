"""SQL values and token boundaries survive comparison normalization."""
import pytest
from llm_wiki.sqlite_schema_text import normalized_schema_sql


@pytest.mark.parametrize(('source', 'expected'), [
    (None, ''), ('', ''), (' \t\r\n\f', ''),
    (' \tCREATE\r\nTABLE T (X TEXT)\f', 'create table t (x text)'),
    ("CHECK(X = 'Ready  Now')", "check(x = 'Ready  Now')"),
    ("SELECT 'It''s  Ready'", "select 'It''s  Ready'"),
    ('SELECT "A""B  C"', 'select "A""B  C"'),
    ('SELECT `A``B  C`', 'select `A``B  C`'),
    ('SELECT [A  B]', 'select [A  B]'),
    ("SELECT 'A\nB\\C'", "select 'A\nB\\C'"),
    ('SELECT ſtate', 'select ſtate'),
    ('SELECT A\u00a0TEXT', 'select a\u00a0text'),
    ('\u00a0SELECT\u00a0', '\u00a0select\u00a0'),
    ('SELECT\vA', 'select\va'),
    ('SELECT -- Keep  CASE\n  A', 'select -- Keep  CASE\n a'),
    ('SELECT -- Keep\r\nA', 'select -- Keep\r\na'),
    ('SELECT -- Keep\rA', 'select -- Keep\ra'),
    ('SELECT -- Keep\n', 'select -- Keep\n'),
    ('SELECT /* Keep  CASE\n */ A', 'select /* Keep  CASE\n */ a'),
    ("SELECT '--X'", "select '--X'"),
    ('SELECT /* "X" */ A', 'select /* "X" */ a'),
    ('SELECT -- End  ', 'select -- End  '),
    ('SELECT /* End  ', 'select /* End  '),
    ("SELECT 'End  ", "select 'End  "),
    ('SELECT "End  ', 'select "End  '),
    ('SELECT `End  ', 'select `End  '),
    ('SELECT [End  ', 'select [End  '),
])
def test_normalization_preserves_values_and_protected_boundaries(source, expected):
    assert normalized_schema_sql(source) == expected
    assert normalized_schema_sql(expected) == expected


@pytest.mark.parametrize(('left', 'right'), [
    ("CHECK(X='ready')", "CHECK(X='READY')"),
    ("CHECK(X='A B')", "CHECK(X='A  B')"),
    ('SELECT state', 'SELECT ſtate'),
    ('SELECT A TEXT', 'SELECT A\u00a0TEXT'),
    ('SELECT -- X\n A', 'SELECT -- X A'),
    ('SELECT "ready"', 'SELECT "READY"'),
])
def test_semantic_changes_never_compare_equal(left, right):
    assert normalized_schema_sql(left) != normalized_schema_sql(right)


@pytest.mark.parametrize('prefix', ["'", '"', '`', '[', '/*', '--'])
def test_long_unterminated_token_keeps_all_contents(prefix):
    # Exercise a substantial unmatched remainder without a machine-speed deadline.
    source = prefix + 'X \t' * 10000
    assert normalized_schema_sql(source) == source
