"""A compiler failure must fit the existing real source-failure byte contract."""
from types import SimpleNamespace

import compile_memory as compiler
import pytest

from tests.test_memory_queue import _v3_queue


@pytest.fixture
def source_queue(tmp_path, monkeypatch):
    queue = _v3_queue(tmp_path)
    monkeypatch.setattr(compiler, 'active_or_legacy_memory_queue', lambda *a, **k: queue)
    source = SimpleNamespace(logical_path='knowledge/daily/2026-11-03.md', sha256='a' * 64)
    return queue, SimpleNamespace(dailies=[source]), tmp_path


@pytest.mark.parametrize(('code', 'expected'), [
    ('ValueError', 'ValueError'),
    ('X' * 64, 'X' * 64),
    ('X' * 80, 'X' * 64),
    ('Я' * 32, 'Я' * 32),
    ('Я' * 80, 'Я' * 32),
    ('文' * 21 + 'X', '文' * 21 + 'X'),
    ('文' * 80, '文' * 21),
    ('😀' * 16, '😀' * 16),
    ('😀' * 80, '😀' * 16),
])
def test_compiler_records_complete_utf8_reason_in_real_v3_queue(source_queue, code, expected):
    queue, inputs, state_root = source_queue
    compiler._record_compile_source_failures(inputs, state_root, error_code=code)
    source = inputs.dailies[0]
    record = queue.source_failure(source.logical_path, source.sha256)
    assert record['error_code'] == expected
    assert record['producer'] == 'compile'
    assert queue.source_failure_keys() == [(source.logical_path, source.sha256)]
    assert len(record['error_code'].encode('utf-8')) <= 64


@pytest.mark.parametrize('code', ['', 'invalid\ncode', 'invalid\rcode'])
def test_invalid_compiler_reason_is_still_refused(source_queue, code):
    queue, inputs, state_root = source_queue
    with pytest.raises(ValueError, match='source failure fields are invalid'):
        compiler._record_compile_source_failures(inputs, state_root, error_code=code)
    assert queue.source_failure_keys() == []
