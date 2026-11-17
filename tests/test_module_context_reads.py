"""Source inspection must not use files omitted from the maintenance packet."""
import json

import pytest

from tests import module_context
from tests.module_context_reads_helpers import _unchecked_reads


def _reader(tmp_path):
    source = tmp_path / 'owned.py'
    source.write_text('value = 1\n')
    manifest = tmp_path / 'context.json'
    manifest.write_text(json.dumps({'files': ['owned.py']}))
    return module_context.context_reader(tmp_path, manifest), source


def test_reader_accepts_declared_source(tmp_path):
    read, source = _reader(tmp_path)
    assert read(source) == 'value = 1\n'


def test_reader_refuses_existing_undeclared_consumer(tmp_path):
    read, _ = _reader(tmp_path)
    consumer = tmp_path / 'consumer.py'
    consumer.write_text('value = 2\n')
    with pytest.raises(AssertionError, match='Undeclared context read'):
        read(consumer)


def test_reader_refuses_external_path(tmp_path):
    read, _ = _reader(tmp_path)
    with pytest.raises(AssertionError, match='Undeclared context read'):
        read(tmp_path.parent / 'external.py')


def test_reader_refuses_symlink_escape(tmp_path):
    read, source = _reader(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + '-outside.py')
    outside.write_text('outside\n')
    source.unlink()
    source.symlink_to(outside)
    with pytest.raises(AssertionError):
        read(source)


@pytest.mark.parametrize('source', [
    "Path('consumer.py').read_text()", "p.read_bytes()", "p.open()", "open(p)",
])
def test_raw_read_guard_detects_original_failure(source):
    assert _unchecked_reads(source)


def test_raw_read_guard_accepts_checked_reader():
    assert not _unchecked_reads('read_source(path)')


def test_shared_boundary_helper_cannot_read_an_undeclared_source(tmp_path):
    from tests.module_boundaries import assert_stdlib_boundary

    read, _ = _reader(tmp_path)
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / 'consumer.py').write_text('import json\n')
    with pytest.raises(AssertionError, match='Undeclared context read'):
        assert_stdlib_boundary(tmp_path, 'consumer', read_source=read)
