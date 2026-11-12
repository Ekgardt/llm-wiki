"""Change construction retains the original content and validation semantics."""
import pytest
from llm_wiki.markdown_records import MarkdownChange, TransactionRecord


@pytest.mark.parametrize('kind', ['create', 'replace'])
def test_content_and_size_bound_are_preserved(kind):
    content = b'\x00\xffmarkdown\n'
    change = getattr(MarkdownChange, kind)('notes/page.md', content, max_before_bytes=123)
    assert change.kind == kind
    assert change.path == 'notes/page.md'
    assert change.content is content
    assert change.max_before_bytes == 123


@pytest.mark.parametrize('kind', ['create', 'replace'])
@pytest.mark.parametrize('content', ['text', bytearray(b'bytes'), None, 42])
def test_factory_rejects_nonbytes(kind, content):
    with pytest.raises(TypeError, match='^Markdown content must be bytes$'):
        getattr(MarkdownChange, kind)('notes/page.md', content)


def test_delete_has_no_content():
    assert MarkdownChange.delete('notes/page.md', max_before_bytes=123) == MarkdownChange(
        'delete', 'notes/page.md', None, 123,
    )


def test_record_mapping_remains_shallow_and_keeps_identity():
    conditions = {'source': 'before'}
    record = TransactionRecord('tx', 'op', 'prepared', (), conditions, 'now', 'now')
    conditions['source'] = 'after'
    assert record.preconditions is conditions
    assert record.preconditions['source'] == 'after'
