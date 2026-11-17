"""Unchanged lexical path assertions from mixed filesystem tests."""
from pathlib import PurePosixPath

import pytest
from llm_wiki.restricted_paths import restricted_relative_path


@pytest.mark.parametrize(
    "value",
    ["", ".", "../x", "a/../../x", "/absolute", "C:/absolute", "a\\b", "a//b"],
)
def test_restricted_relative_path_rejects_unsafe_values(value):
    with pytest.raises(ValueError):
        restricted_relative_path(value, ("knowledge", "run/transactions"))


def test_restricted_relative_path_accepts_only_allowed_roots():
    assert restricted_relative_path("knowledge/daily/2026-01-01.md", ("knowledge",)) == (
        PurePosixPath("knowledge/daily/2026-01-01.md")
    )
    with pytest.raises(ValueError, match="allowed root"):
        restricted_relative_path("cache/private.json", ("knowledge",))


@pytest.mark.parametrize('value', ['knowledge-private/a', 'knowledge2/a', 'Knowledge/a'])
def test_similar_prefix_is_not_a_member_of_the_allowed_root(value):
    with pytest.raises(ValueError, match='allowed root'):
        restricted_relative_path(value, ('knowledge',))


def test_exact_root_is_allowed_but_empty_allowlist_refuses():
    assert restricted_relative_path('knowledge', ('knowledge',)) == PurePosixPath('knowledge')
    with pytest.raises(ValueError, match='allowed root'):
        restricted_relative_path('knowledge/a.md', ())
