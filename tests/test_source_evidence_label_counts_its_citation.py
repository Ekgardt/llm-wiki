"""The documented source/evidence label counts only with a citation."""
import lint_memory
import pytest

from tests.test_a_short_page_still_needs_evidence import _page


@pytest.mark.parametrize('citation', [
    'Source / Evidence: owner decision.\n',
    'Source/Evidence: `docs/decision.md`.\n',
    'Source / Evidence:\n- `docs/decision.md`\n',
    'Source / Evidence:\r\n+ owner decision\r\n',
    'Source / Evidence:\n\n* [[accepted-decision]]\n',
])
def test_combined_label_has_real_inline_or_list_content(tmp_path, monkeypatch, citation):
    page = _page(tmp_path, monkeypatch, 'One claim.\n' + citation)
    assert lint_memory.check_missing_sources_section([page]) == []


@pytest.mark.parametrize('citation', [
    'Source / Evidence:',
    'Source / Evidence:   \n',
    'Source / Evidence:\n-   \n',
    'Source / Evidence:\n\n*\n',
    'Source / Evidence:\n## Unrelated\n- another item\n',
    '> Source / Evidence: quoted label\n',
    '    Source / Evidence: code-indented label\n',
])
def test_empty_or_unrelated_combined_label_stays_missing(tmp_path, monkeypatch, citation):
    page = _page(tmp_path, monkeypatch, 'One claim.\n' + citation)
    assert lint_memory.check_missing_sources_section([page]) == ['note.md']
