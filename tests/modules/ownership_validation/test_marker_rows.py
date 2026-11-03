"""Real SQLite rows preserve NULL, blob and canonical marker contracts."""
import hashlib
import sqlite3

import pytest
from llm_wiki import ownership_validation as owner
from llm_wiki.ownership_contracts import MarkerIdentity, OperationalOwnershipError
from llm_wiki.runtime_contracts import RuntimeFileIdentity

IDENTITY = RuntimeFileIdentity('posix', '1', '2', 8, 123)
MARKER = MarkerIdentity('run/marker.json', 'a' * 64, IDENTITY, 42)


def row(path, digest, content):
    with sqlite3.connect(':memory:') as db:
        db.row_factory = sqlite3.Row
        return db.execute('SELECT ? AS marker_path, ? AS marker_sha256, ? AS marker_identity_json',
                          (path, digest, content)).fetchone()


def test_absent_marker_is_three_null_columns():
    assert owner._marker_from_row(row(None, None, None)) is None


def test_canonical_blob_roundtrip():
    raw = owner._marker_json(MARKER)
    assert owner._marker_from_row(row(MARKER.relative_path, MARKER.sha256, raw)) == MARKER


@pytest.mark.parametrize('columns', [
    (None, 'digest', b'{}'), ('run/x', None, b'{}'), ('run/x', 'digest', '{}'),
    (1, 'digest', b'{}'), ('run/x', 1, b'{}'), ('run/x', 'digest', None),
])
def test_partial_or_mistyped_columns_are_invalid(columns):
    with pytest.raises(OperationalOwnershipError, match='^owner_record_invalid$'):
        owner._marker_from_row(row(*columns))


@pytest.mark.parametrize('raw', [b'{', b'{}', b'null', b'[]', b'{"file_identity":null,"pid":42}',
                                b'{"file_identity":{},"pid":42}'])
def test_malformed_or_incomplete_marker_is_invalid(raw):
    with pytest.raises(OperationalOwnershipError, match='^owner_record_invalid$'):
        owner._marker_from_columns('run/x', 'a' * 64, raw)


def test_equivalent_noncanonical_json_is_refused():
    raw = owner._marker_json(MARKER)
    with pytest.raises(OperationalOwnershipError, match='^owner_record_invalid$'):
        owner._marker_from_columns(MARKER.relative_path, MARKER.sha256, b' ' + raw)


def test_supplied_content_and_both_identities_must_match():
    content = b'original'
    marker = MarkerIdentity('run/x', hashlib.sha256(content).hexdigest(), IDENTITY, 42)
    assert owner._require_marker_match(IDENTITY, IDENTITY, content, marker) is None
    with pytest.raises(OperationalOwnershipError, match='^marker_identity_invalid$'):
        owner._require_marker_match(IDENTITY, None, content, marker)
    with pytest.raises(OperationalOwnershipError, match='^marker_identity_invalid$'):
        owner._require_marker_match(IDENTITY, IDENTITY, b'modified', marker)
