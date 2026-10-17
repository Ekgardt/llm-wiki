"""A blocked dependent release must not delete its canonical owner."""
import sqlite3

import capture_adoption
import integration_adapter as adapter
import pytest

from tests.test_queue_v3_capture_links import _coordinator, _queue


@pytest.mark.parametrize("context", [adapter._capture_publication_fence, capture_adoption._fresh_capture_authority])
def test_failed_fence_release_keeps_original_error_and_owner(tmp_path, monkeypatch, context):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    failure = sqlite3.OperationalError("database is locked")

    def locked(_fence):
        raise failure

    monkeypatch.setattr(coordinator, "release_intent_fence", locked)
    with pytest.raises(sqlite3.OperationalError) as caught:
        with context(queue, coordinator, "d" * 64):
            pass
    assert caught.value is failure
    with coordinator._connect() as database:
        assert database.execute("SELECT COUNT(*) FROM intent_fences").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM maintenance_owners WHERE role='capture'").fetchone()[0] == 1
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("context", [adapter._capture_publication_fence, capture_adoption._fresh_capture_authority])
def test_failed_fence_acquisition_releases_unreferenced_owner(tmp_path, monkeypatch, context):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    failure = sqlite3.OperationalError("database is locked")

    def locked(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(coordinator, "acquire_intent_fence", locked)
    with pytest.raises(sqlite3.OperationalError) as caught:
        with context(queue, coordinator, "e" * 64):
            pytest.fail("an unacquired fence cannot publish")
    assert caught.value is failure
    with coordinator._connect() as database:
        assert database.execute("SELECT COUNT(*) FROM intent_fences").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM maintenance_owners WHERE role='capture'").fetchone()[0] == 0
