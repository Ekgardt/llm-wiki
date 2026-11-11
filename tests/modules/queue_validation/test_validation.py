"""Queue argument boundaries retain their original legacy/adopted differences."""
import pytest
from llm_wiki import queue_validation as check
from llm_wiki.queue_contracts import ResultConflictError


@pytest.mark.parametrize('kind', ['', None, 'я' * 33])
def test_adopted_task_kind_is_bounded_in_utf8(kind):
    with pytest.raises(ValueError, match='kind'):
        check.require_enqueue_arguments(kind, 1, 0, None)


def test_legacy_and_adopted_kind_bounds_are_intentionally_distinct():
    check.require_enqueue_arguments('я' * 32, 1, 0, None)
    check.require_legacy_enqueue_arguments('я' * 100, 1, 0, None)
    with pytest.raises(ValueError, match='kind'):
        check.require_enqueue_arguments('я' * 100, 1, 0, None)


@pytest.mark.parametrize('version,priority', [(True, 0), (0, 0), (2147483648, 0), (1, True), (1, 101), (1, -101)])
def test_integer_ranges_reject_booleans_and_out_of_range_values(version, priority):
    with pytest.raises(ValueError):
        check.require_enqueue_arguments('query', version, priority, None)


def test_dedupe_bound_is_utf8_bytes():
    check.require_enqueue_arguments('query', 1, 0, 'я' * 256)
    with pytest.raises(ValueError, match='dedupe_key'):
        check.require_enqueue_arguments('query', 1, 0, 'я' * 257)


@pytest.mark.parametrize('lease,heartbeat', [(True, 1), (2, True), (0, 1), (2, 2), (2, 3)])
def test_worker_heartbeat_must_fit_positive_integer_lease(lease, heartbeat):
    with pytest.raises(ValueError):
        check.validate_worker_policy(lease, heartbeat, 8, 1, 60)


def test_worker_retry_window_preserves_order_and_attempt_bound():
    check.validate_worker_policy(2, 1, 100, 1, 60)
    with pytest.raises(ValueError, match='max_attempts'):
        check.validate_worker_policy(2, 1, 101, 1, 60)
    with pytest.raises(ValueError, match='must not exceed'):
        check.validate_worker_policy(2, 1, 8, 60, 1)


def test_result_operation_cannot_be_rebound():
    check.check_result_operation({'result_operation_id': None}, 'new')
    check.check_result_operation({'result_operation_id': 'same'}, 'same')
    with pytest.raises(ResultConflictError):
        check.check_result_operation({'result_operation_id': 'other'}, 'new')


def test_result_request_keeps_type_and_empty_operation_checks():
    check.check_result_request('operation', b'')
    with pytest.raises(TypeError):
        check.check_result_request('operation', 'not bytes')
    with pytest.raises(ValueError):
        check.check_result_request('', b'bytes')
