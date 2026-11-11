"""Runtime records and exceptions have no resource-owning behavior."""
from dataclasses import FrozenInstanceError, asdict

import pytest
from llm_wiki.runtime_contracts import (
    MetadataDurabilityUnavailable,
    RuntimeFileIdentity,
    UnsafeStateRoot,
)


def test_identity_is_the_frozen_five_field_record():
    identity = RuntimeFileIdentity('posix', 'volume', 'inode', 9, 10)
    assert asdict(identity) == dict(platform='posix', volume='volume', file_id='inode', size=9, mtime_ns=10)
    assert identity == RuntimeFileIdentity('posix', 'volume', 'inode', 9, 10)
    with pytest.raises(FrozenInstanceError):
        identity.size = 11


def test_exceptions_retain_category_and_error_code():
    assert isinstance(UnsafeStateRoot('unsafe'), ValueError)
    error = MetadataDurabilityUnavailable(5, 'not durable')
    assert isinstance(error, OSError)
    assert (error.errno, error.code) == (5, 'metadata_durability_unavailable')
