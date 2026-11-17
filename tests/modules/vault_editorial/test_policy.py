"""Editorial names stay distinct; supplied paths use actual resolution."""
from pathlib import Path

import pytest
from vault_editorial import (
    BACKLINK_EXEMPT_NAMES,
    BROKEN_LINK_SKIP_NAMES,
    EDITORIAL_NAMES,
    editorial_parents_to_skip,
)
from vault_log import LOG_NAME, LOG_RELATIVE


def test_editorial_sets_are_immutable_and_have_distinct_meanings():
    assert isinstance(EDITORIAL_NAMES, frozenset)
    assert isinstance(BACKLINK_EXEMPT_NAMES, frozenset)
    assert isinstance(BROKEN_LINK_SKIP_NAMES, frozenset)
    assert 'README.md' in EDITORIAL_NAMES
    assert 'README.md' not in BACKLINK_EXEMPT_NAMES
    assert 'README.md' not in BROKEN_LINK_SKIP_NAMES
    assert 'Ingestion Workflow.md' in BACKLINK_EXEMPT_NAMES
    assert 'Ingestion Workflow.md' not in EDITORIAL_NAMES


@pytest.mark.parametrize('name', ['readme.md', 'STATE.md', 'ordinary-page.md'])
def test_editorial_membership_preserves_case_and_ordinary_content(name):
    assert name not in EDITORIAL_NAMES


def test_template_resolution_is_relative_to_the_supplied_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expected = tmp_path / 'knowledge' / 'projects' / '_template'
    assert editorial_parents_to_skip(Path('knowledge')) == (expected,)
    assert not expected.exists()


def test_existing_template_path_has_the_same_resolution(tmp_path):
    expected = tmp_path / 'projects' / '_template'
    expected.mkdir(parents=True)
    assert editorial_parents_to_skip(tmp_path) == (expected.resolve(),)


def test_log_name_and_relative_path_select_the_same_private_file():
    assert Path(LOG_RELATIVE) == Path('knowledge') / LOG_NAME
    assert LOG_NAME != 'log.md'
