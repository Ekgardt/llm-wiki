"""Real page reads preserve declarations without claiming entailment."""
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import page_facts
import pytest


def _page(root: Path, text: str) -> Path:
    path = root / 'page.md'
    path.write_text(text, encoding='utf-8')
    return path


def test_declared_facts_are_normalized_and_do_not_modify_the_page(tmp_path):
    path = _page(tmp_path, '''---
type: "Decision"
source_authority: 'USER'
confidence: HIGH
status: Accepted
---
A claim whose truth this reader does not verify.
''')
    before = path.read_bytes()
    facts = page_facts.read_page_facts(tmp_path, 'page.md')
    assert (facts.page_type, facts.authority, facts.confidence, facts.status) == (
        'decision', 'user', 'high', 'accepted',
    )
    assert facts.relative_path == 'page.md'
    assert facts.trust_weight == 1.6875  # Existing user (1.35) / decision (1.25) policy.
    assert facts.stated_confidence() == 0.9
    assert facts.aging is False
    assert path.read_bytes() == before
    with pytest.raises(FrozenInstanceError):
        facts.status = 'superseded'


@pytest.mark.parametrize('text', ['No frontmatter', '---\ntype: concept\nUnclosed'])
def test_missing_frontmatter_keeps_unknown_fields_and_default_status(tmp_path, text):
    _page(tmp_path, text)
    facts = page_facts.read_page_facts(tmp_path, 'page.md')
    assert (facts.page_type, facts.authority, facts.confidence) == ('', '', '')
    assert facts.status == 'active'
    assert facts.stated_confidence() == 0.6


@pytest.mark.parametrize(('confidence', 'expected'), [
    ('high', 0.9), ('medium', 0.7), ('low', 0.4), ('unrecognized', 0.6),
])
def test_confidence_reports_the_declared_value(tmp_path, confidence, expected):
    _page(tmp_path, f'---\nconfidence: {confidence}\n---\n')
    assert page_facts.read_page_facts(tmp_path, 'page.md').stated_confidence() == expected


@pytest.mark.parametrize(('kind', 'days', 'age', 'aging'), [
    ('debugging', 60, 60, False), ('debugging', 61, 61, True),
    ('concept', 1000, 1000, False), ('debugging', -1, 0, False),
])
def test_age_uses_mtime_and_the_existing_type_window(tmp_path, monkeypatch, kind, days, age, aging):
    path = _page(tmp_path, f'---\ntype: {kind}\n---\n')
    now = 2_000_000_000
    modified = now - days * 86400
    os.utime(path, (modified, modified))
    monkeypatch.setattr(page_facts.time, 'time', lambda: now)
    facts = page_facts.read_page_facts(tmp_path, 'page.md')
    assert facts.age_days == age
    assert facts.aging is aging


def test_failed_stat_leaves_age_unknown_after_a_successful_read(tmp_path, monkeypatch):
    _page(tmp_path, '---\ntype: debugging\n---\n')

    def unavailable(_path, **_kwargs):
        raise OSError('stat unavailable')

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'stat', unavailable)
        facts = page_facts.read_page_facts(tmp_path, 'page.md')
    assert facts.age_days is None
    assert facts.aging is False


@pytest.mark.parametrize('relative', ['', 'absent.md', 'directory'])
def test_unreadable_page_has_no_facts(tmp_path, relative):
    (tmp_path / 'directory').mkdir()
    assert page_facts.read_page_facts(tmp_path, relative) is None


def test_existing_head_limit_counts_decoded_characters(tmp_path):
    prefix = '---\nextra: ' + 'я' * 5000 + '\nconfidence: low\n---\n'
    assert len(prefix.encode('utf-8')) > page_facts.MAX_FRONTMATTER_BYTES
    path = _page(tmp_path, prefix + 'x' * 9000)
    assert len(page_facts._read_head(path)) == page_facts.MAX_FRONTMATTER_BYTES
    assert page_facts.read_page_facts(tmp_path, 'page.md').stated_confidence() == 0.4


def test_frontmatter_beyond_existing_head_limit_is_not_claimed(tmp_path):
    _page(tmp_path, '---\nextra: ' + 'x' * 8192 + '\nconfidence: high\n---\n')
    assert page_facts.read_page_facts(tmp_path, 'page.md').stated_confidence() == 0.6
