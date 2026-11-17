"""Original wire bytes and path/record rejection at the document boundary."""
import json
from pathlib import Path

import pytest
from llm_wiki import breadcrumb_documents as documents

from tests.module_context import context_reader
from tests.modules.breadcrumb_documents.contract_helpers import _source

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/breadcrumb-documents-context.json')
BASELINE = json.loads(read_source(Path(__file__).with_name('render_baseline.json')))


@pytest.mark.parametrize('case', BASELINE['samples'])
def test_complete_original_renderer_bytes(case):
    head, sources = _source(case['content'].encode())
    assert head == case['head']
    assert list(sources.items()) == [(p, text.encode()) for p, text in case['documents']]
    assert documents.restore_source(head, sources.__getitem__) == case['content'].encode()


def test_archive_path_preserves_date_identity_and_name():
    head, _ = _source(b'whole input')
    assert documents.archived_source_path(head) == head.replace(
        'sessions/2026-09-29/', 'sessions/archive/2026-09/2026-09-29/',
    )


@pytest.mark.parametrize('path', [
    'elsewhere/2026-09-29/a.md', 'knowledge/raw/sessions/2026-02-30/a.md',
    'knowledge/raw/sessions/archive/2026-09/2026-09-29/a.md',
])
def test_noncanonical_archive_sources_are_refused(path):
    with pytest.raises(ValueError):
        documents.archived_source_path(path)


def test_rewritten_visible_head_cannot_hide_unchanged_integrity_record():
    head, sources = _source(b'complete input')
    sources[head] = sources[head].replace(b'# Complete captured event', b'# forged title')
    with pytest.raises(ValueError, match='changed or conflicts'):
        documents.restore_source(head, sources.__getitem__)


def test_non_digest_filename_is_not_a_reserved_evidence_document():
    assert documents.is_breadcrumb_document(Path('a' * 64 + '.breadcrumb.md'))
    assert documents.is_breadcrumb_document(Path('b' * 64 + '.breadcrumb-part.md'))
    assert not documents.is_breadcrumb_document(Path('human-session.md'))
    assert not documents.is_breadcrumb_document(Path('a' * 63 + '.breadcrumb.md'))
