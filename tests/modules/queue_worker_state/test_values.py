"""Worker values and counters, first checked against the original queue owner."""
import pytest
from llm_wiki.queue_contracts import DeferredResult, QueueOperationError
from llm_wiki.queue_worker_state import (
    ProcessorOutcome,
    QueueRetryPolicy,
    WorkerProgress,
    processor_error_code,
)


@pytest.mark.parametrize(('error', 'expected'), [
    (QueueOperationError('fence_lost', 'private text'), 'fence_lost'),
    (RuntimeError('fence_lost'), 'processor_failed:fence_lost'),
    (RuntimeError('/private/path token=hidden'), 'processor_failed:RuntimeError'),
    (ValueError(''), 'processor_failed:ValueError'),
    (RuntimeError('UPPER'), 'processor_failed:RuntimeError'),
])
def test_reason_policy_preserves_codes_and_hides_arbitrary_message(error, expected):
    assert processor_error_code(error) == expected


def test_long_ascii_class_name_is_clipped_at_existing_bound():
    kind = type('Long' * 30, (Exception,), {})
    assert processor_error_code(kind('private text')) == ('processor_failed:' + kind.__name__)[:64]


def test_retry_values_keep_tuple_and_replace_contract():
    policy = QueueRetryPolicy(7, 11, 19)
    assert tuple(policy) == (7, 11, 19)
    assert policy._replace(max_attempts=8) == QueueRetryPolicy(8, 11, 19)
    with pytest.raises(AttributeError):
        policy.max_attempts = 9


def test_retry_defaults_are_the_shared_existing_defaults():
    from llm_wiki.reliability_defaults import DEFAULTS
    assert tuple(QueueRetryPolicy()) == (
        DEFAULTS.queue_max_attempts, DEFAULTS.retry_base_seconds, DEFAULTS.retry_cap_seconds,
    )


def test_outcome_retains_deferred_value_and_default_reason():
    deferred = DeferredResult(b'bytes')
    outcome = ProcessorOutcome(deferred, False, False)
    assert outcome.outcome is deferred
    assert outcome.error_code is None
    assert tuple(outcome) == (deferred, False, False, None)


@pytest.mark.parametrize(('counts', 'expected'), [
    ({'ok': 1, 'failed': 2, 'dead': 1, 'skipped': 3}, 'worked'),
    ({'ok': 1, 'failed': 2, 'dead': 1, 'skipped': 3, 'halted': True}, 'halted'),
])
def test_progress_accumulates_and_resets_idle_only_after_work(counts, expected):
    progress = WorkerProgress({'ok': 2, 'failed': 3, 'dead': 4, 'skipped': 5}, 6, 10.0)
    assert progress.advance(counts) == expected
    assert progress.totals == {'ok': 3, 'failed': 5, 'dead': 5, 'skipped': 8}
    assert progress.processed == 9
    assert progress.idle_started is None


def test_idle_progress_keeps_clock_and_processed_count():
    progress = WorkerProgress({'ok': 0, 'failed': 0, 'dead': 0, 'skipped': 0}, 4, 10.0)
    assert progress.advance({'ok': 0, 'failed': 0, 'dead': 2, 'skipped': 1, 'halted': True}) == 'idle'
    assert (progress.processed, progress.idle_started) == (4, 10.0)
    assert progress.totals == {'ok': 0, 'failed': 0, 'dead': 2, 'skipped': 1}


@pytest.mark.parametrize(('name', 'expected'), [
    ('Я' * 80, 'processor_failed:' + 'Я' * 23),
    ('文' * 80, 'processor_failed:' + '文' * 15),
    ('😀' * 80, 'processor_failed:' + '😀' * 11),
])
def test_unicode_type_reason_fits_stored_bytes(name, expected):
    kind = type(name, (Exception,), {})
    code = processor_error_code(kind('private text must not enter the reason'))
    assert code == expected
    assert len(code.encode('utf-8')) <= 64
