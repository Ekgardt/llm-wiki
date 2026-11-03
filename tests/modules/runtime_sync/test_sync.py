"""A metadata sync failure remains visible and the acquired descriptor is closed."""
import errno
import os

import pytest
from llm_wiki import runtime_sync as sync
from llm_wiki.runtime_contracts import MetadataDurabilityUnavailable


@pytest.mark.skipif(os.name != 'posix', reason='POSIX directory descriptor lifecycle')
def test_directory_sync_failure_closes_the_same_acquired_descriptor(tmp_path, monkeypatch):
    closed = []
    original_close = os.close

    def failed(descriptor):
        raise OSError(errno.EIO, 'flush failed')

    def close(descriptor):
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(sync.os, 'fsync', failed)
    monkeypatch.setattr(sync.os, 'close', close)
    with pytest.raises(MetadataDurabilityUnavailable) as error:
        sync.fsync_directory(tmp_path)
    assert error.value.errno == errno.EIO
    assert len(closed) == 1
    with pytest.raises(OSError):
        os.fstat(closed[0])


def test_metadata_error_keeps_native_error_number():
    native = OSError(5, 'native failure')
    native.winerror = 32
    error = sync._metadata_durability_error('operation', native)
    assert (error.errno, error.winerror) == (5, 32)
