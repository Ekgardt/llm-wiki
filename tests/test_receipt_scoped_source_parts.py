"""Fallback receipt lookup must own only a source scope and current parts."""
import compile_memory as cm
import pytest
from bounded_io import VerifiedByteRange

from tests.test_compile_receipt_source_lifetime import _daily
from tests.test_compile_receipt_source_lifetime import selection as selection


def test_receipt_digest_lookup_never_uses_eager_source_or_part_list(selection, monkeypatch):
    logical, digest = _daily(selection, '2026-10-20', b'Complete evidence\n')

    def refuse(*args, **kwargs):
        raise AssertionError('whole source or part list retained')

    monkeypatch.setattr(cm, '_read_daily_source', refuse)
    monkeypatch.setattr(cm, '_daily_parts', refuse)
    assert not selection(logical, digest)


def test_receipt_parts_have_one_scope_and_close_on_early_exit(selection):
    logical, _ = _daily(selection, '2026-10-20', b'Complete evidence\n')
    with selection._source_parts(logical) as parts:
        first = next(parts)
        source = first.original_content
        assert type(source) is VerifiedByteRange
        assert first.content == b'Complete evidence\n'
    assert source.stream.closed
    assert selection.prefix_source is None


def test_receipt_scope_rejects_source_mutation_before_result(selection):
    logical, _ = _daily(selection, '2026-10-20', b'Complete evidence\n')
    with pytest.raises((PermissionError, ValueError), match='changed'):
        with selection._source_parts(logical) as parts:
            next(parts)
            (selection.vault / logical).write_bytes(b'Changed evidence\n')


def test_empty_digest_match_is_not_success(selection):
    logical, _ = _daily(selection, '2026-10-20', b'Complete evidence\n')
    assert not selection(logical, '0' * 64)
