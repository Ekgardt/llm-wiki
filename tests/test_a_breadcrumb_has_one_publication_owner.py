"""Complete breadcrumb acceptance and registration share one fenced capture owner."""
import operational_ownership

from tests.test_breadcrumb_storage import _bundle, _publish
from tests.test_queue_v3_capture_links import _coordinator, _queue


def test_publication_acquires_one_owner_for_the_same_occurrence(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    acquire = operational_ownership.OwnershipRegistry.acquire
    requests = []

    def observed(self, role, **kwargs):
        requests.append((role, kwargs['scope']))
        return acquire(self, role, **kwargs)

    monkeypatch.setattr(operational_ownership.OwnershipRegistry, 'acquire', observed)
    result = _publish(queue, coordinator)

    assert result.registered
    assert requests == [('capture', 'intent:' + result.intent_id)]
    assert _bundle(tmp_path, result.intent_id).content


def test_registration_failure_keeps_complete_evidence_and_releases_owner(tmp_path, monkeypatch):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)

    def refused(*args, **kwargs):
        raise RuntimeError('registration refused')

    monkeypatch.setattr(queue, 'enqueue_capture_task_replay_safe', refused)
    result = _publish(queue, coordinator)

    assert not result.registered
    assert 'registration refused' in result.registration_error
    assert _bundle(tmp_path, result.intent_id).content
    with coordinator._connect() as database:
        assert database.execute("SELECT COUNT(*) FROM maintenance_owners WHERE role='capture'").fetchone()[0] == 0
        assert database.execute('SELECT COUNT(*) FROM intent_fences').fetchone()[0] == 0


def _attempt_owned_registration(queue, coordinator, intent_id, ownership):
    import integration_adapter as adapter

    pending, ready = adapter._capture_relative_paths(intent_id)
    adapter._publish_capture_files_and_task(
        queue, coordinator, intent_id=intent_id, payload=b'not published',
        intent_sha256='0' * 64, pending_relative=pending, ready_relative=ready,
        ownership=ownership,
    )


def test_a_forged_fence_cannot_publish_any_bytes(tmp_path):
    from dataclasses import replace

    import integration_adapter as adapter
    import pytest

    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    identity = 'a' * 64
    adapter._ensure_capture_intent_directories(tmp_path, identity)
    with adapter._capture_publication_fence(queue, coordinator, identity) as (owner, fence):
        with pytest.raises(RuntimeError, match='intent_fence_lost'):
            _attempt_owned_registration(queue, coordinator, identity, (owner, replace(fence, token='forged')))
    pending, ready = adapter._capture_relative_paths(identity)
    assert not (tmp_path / pending).exists()
    assert not (tmp_path / ready).exists()


def test_another_runtime_root_cannot_borrow_an_owner(tmp_path):
    import integration_adapter as adapter
    import pytest

    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    foreign = _queue(tmp_path / 'foreign')
    identity = 'b' * 64
    adapter._ensure_capture_intent_directories(foreign.state_root, identity)
    with adapter._capture_publication_fence(queue, coordinator, identity) as ownership:
        with pytest.raises(ValueError, match='runtime roots differ'):
            _attempt_owned_registration(foreign, coordinator, identity, ownership)
    pending, ready = adapter._capture_relative_paths(identity)
    assert not (foreign.state_root / pending).exists()
    assert not (foreign.state_root / ready).exists()


def test_a_forged_owner_cannot_publish_any_bytes(tmp_path):
    from dataclasses import replace

    import integration_adapter as adapter
    import pytest

    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    identity = 'c' * 64
    adapter._ensure_capture_intent_directories(tmp_path, identity)
    with adapter._capture_publication_fence(queue, coordinator, identity) as (owner, fence):
        with pytest.raises(operational_ownership.OperationalOwnershipError, match='owner_fence_lost'):
            _attempt_owned_registration(queue, coordinator, identity, (replace(owner, token='forged'), fence))
    pending, ready = adapter._capture_relative_paths(identity)
    assert not (tmp_path / pending).exists()
    assert not (tmp_path / ready).exists()
