"""Original no-descriptor regression with unchanged assertions and AST."""
import pytest
from llm_wiki import runtime_metadata as records


def test_database_validation_never_opens_or_closes_a_file_descriptor(tmp_path, monkeypatch):
    path = tmp_path / 'database'
    path.write_bytes(b'header')
    path.chmod(0o600)

    def forbidden(*args, **kwargs):
        pytest.fail('metadata-only validation opened or closed a descriptor')

    monkeypatch.setattr(records.os, 'open', forbidden)
    monkeypatch.setattr(records.os, 'close', forbidden)
    assert records.validate_operational_db_file(path, tmp_path, max_bytes=None).st_size == 6
