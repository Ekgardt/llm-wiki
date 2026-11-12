"""Each owner test releases only its own process-local readers."""
import evidence_reader_cache
import pytest


@pytest.fixture(autouse=True)
def _isolated_cache():
    evidence_reader_cache.clear()
    yield
    evidence_reader_cache.clear()
