"""Strict complete-scalar prefixes at every byte boundary."""
import pytest
from llm_wiki.utf8_text import utf8_prefix


@pytest.mark.parametrize(('value', 'ceiling', 'expected'), [
    ('', 0, ''), ('abc', 0, ''), ('abc', 2, 'ab'), ('abc', 3, 'abc'),
    ('abc', 5, 'abc'), ('éx', 1, ''), ('éx', 2, 'é'), ('éx', 3, 'éx'),
    ('文x', 2, ''), ('文x', 3, '文'), ('文x', 4, '文x'),
    ('😀x', 3, ''), ('😀x', 4, '😀'), ('😀x', 5, '😀x'),
    ('aé文😀z', 8, 'aé文'), ('e\u0301x', 2, 'e'),
])
def test_strict_prefix(value, ceiling, expected):
    assert utf8_prefix(value, ceiling) == expected


@pytest.mark.parametrize('ceiling', [0, 1, 64])
def test_invalid_original_unicode_is_not_hidden_by_truncation(ceiling):
    with pytest.raises(UnicodeEncodeError):
        utf8_prefix('a\ud800b', ceiling)
