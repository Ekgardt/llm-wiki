"""Capture preconditions preserve paths, byte limits, identity and fence roles."""
from types import SimpleNamespace

import pytest
from llm_wiki import queue_validation as check
from llm_wiki.queue_contracts import QueueOperationError

INTENT = 'a' * 64
DIGEST = 'b' * 64
PENDING = f'run/capture-intents/pending/aa/{INTENT}.json'


class _Integer(int):
    pass


def test_pending_descriptor_binds_identity_to_its_exact_path():
    check._require_capture_intent_descriptor(INTENT, PENDING, DIGEST, 1)
    with pytest.raises(ValueError, match='descriptor'):
        check._require_capture_intent_descriptor('c' * 64, PENDING, DIGEST, 1)


@pytest.mark.parametrize('path', [PENDING.replace('/pending/', '/ready/'),
                                 '/'+PENDING, PENDING.replace('/', '\\'),
                                 'run/capture-intents/pending/../escape.json'])
def test_pending_paths_reject_wrong_stage_and_noncanonical_names(path):
    with pytest.raises(ValueError):
        check._require_capture_intent_path(path, 'pending')


@pytest.mark.parametrize('digest', ['', 'A' * 64, 'a' * 63, 'g' * 64])
def test_both_digest_entry_points_reject_noncanonical_hex(digest):
    with pytest.raises(ValueError, match='lowercase 64-hex'):
        check._require_lower_sha256(digest, 'identity')
    with pytest.raises(ValueError, match='lowercase 64-hex'):
        check._check_capture_intent_digest(digest, 'identity')


@pytest.mark.parametrize('size', [True, 0, -1, 1048577])
def test_both_descriptor_size_contracts_keep_the_existing_payload_bound(size):
    with pytest.raises(ValueError, match='byte size'):
        check._require_capture_intent_size(size)
    with pytest.raises(ValueError, match='byte size'):
        check._check_capture_intent_size(size)


def test_pending_and_publication_integer_contracts_remain_distinct():
    check._check_capture_intent_descriptor(INTENT, 'run/capture-intents/imported.json', DIGEST, _Integer(1))
    with pytest.raises(ValueError, match='byte size'):
        check._require_capture_intent_size(_Integer(1))


def test_decision_path_keeps_its_baseline_canonical_digest():
    expected = 'run/queue-results/capture-decision-cb29132873a6854664e0d3d709812ea11a7ef827daa95f5b9b22d8ef838a0c82.json'
    assert check._capture_decision_relative_path(INTENT, 'flush') == expected
    check._require_capture_decision_path(expected, INTENT, 'flush')
    with pytest.raises(ValueError, match='decision path'):
        check._require_capture_decision_path(expected, INTENT, 'feedback')


def test_terminal_path_and_dispositions_keep_their_wire_contract():
    check._require_capture_terminal_path(f'run/queue-results/capture-{INTENT}.json', INTENT)
    with pytest.raises(ValueError, match='terminal path'):
        check._require_capture_terminal_path('run/queue-results/other.json', INTENT)
    assert check._capture_terminal_disposition_fields('markdown_committed') == {
        'kind', 'transaction_id', 'operation_id', 'decision_sha256', 'outputs'}
    assert check._capture_terminal_disposition_fields('no_durable_content') == {'kind', 'decision_sha256'}


@pytest.mark.parametrize('kind', [None, [], 'unknown'])
def test_unknown_terminal_disposition_has_no_invented_fields(kind):
    assert check._capture_terminal_disposition_fields(kind) is None


@pytest.mark.parametrize('path', [None, True, 'я' * 2049])
def test_capture_identity_requires_bounded_text(path):
    with pytest.raises(ValueError, match='intent_path'):
        check._require_capture_identity(INTENT, DIGEST, path)


def test_capture_and_worker_fence_roles_cannot_be_interchanged():
    owner = SimpleNamespace(role='worker')
    fence = SimpleNamespace(intent_id=INTENT, mode='capture', owner=owner)
    check._require_capture_fence(fence, INTENT, owner, SimpleNamespace)
    with pytest.raises(ValueError, match='intent fence'):
        check._require_worker_intent_fence(fence, INTENT, SimpleNamespace)
    fence.mode = 'worker'
    check._require_worker_intent_fence(fence, INTENT, SimpleNamespace)
    with pytest.raises(ValueError, match='capture fence'):
        check._require_capture_fence(fence, INTENT, owner, SimpleNamespace)


def test_decision_owner_and_repair_role_are_checked():
    owner = SimpleNamespace(role='repair')
    task = SimpleNamespace(owner=owner)
    intent = SimpleNamespace(owner=owner)
    check._require_matching_decision_owner(owner, task, intent, SimpleNamespace)
    check._require_repair_owner(owner, SimpleNamespace, 'repair required')
    with pytest.raises(ValueError, match='semantic decision owner'):
        check._require_matching_decision_owner(SimpleNamespace(role='other'), task, intent, SimpleNamespace)
    with pytest.raises(ValueError, match='repair required'):
        check._require_repair_owner(SimpleNamespace(role='other'), SimpleNamespace, 'repair required')


@pytest.mark.parametrize('stage,digest', [('other', DIGEST), ('flush', 'B' * 64)])
def test_semantic_decision_shape_rejects_unknown_stage_or_digest(stage, digest):
    with pytest.raises(ValueError):
        check._require_semantic_decision_shape(stage, digest)


def test_scan_cursor_preserves_empty_and_explicit_text_pair():
    assert check._capture_scan_cursor(None) == ('', '')
    assert check._capture_scan_cursor(('stamp', 'id')) == ('stamp', 'id')


@pytest.mark.parametrize('cursor', [[], ('one',), ('stamp', 1), (True, 'id')])
def test_scan_cursor_rejects_wrong_shape_or_field_types(cursor):
    with pytest.raises(ValueError, match='capture scan cursor'):
        check._capture_scan_cursor(cursor)


def test_ready_republication_must_match_all_stored_fields():
    row = dict(relative_path=PENDING, intent_sha256=DIGEST, byte_size=1, publication_state='ready')
    check._check_ready_intent_identity(row, PENDING, DIGEST, 1)
    with pytest.raises(QueueOperationError, match='capture_intent_conflict'):
        check._check_ready_intent_identity(row, PENDING, DIGEST, 2)


@pytest.mark.parametrize('kind', ['transaction', 'terminal', 'corrupt-disposition'])
def test_seal_consumer_kinds_remain_closed(kind):
    check._check_consumer_kind(kind)
    with pytest.raises(ValueError, match='consumer kind'):
        check._check_consumer_kind('unknown')


def test_identifiers_and_reasons_are_bounded_by_utf8_bytes():
    check._require_task_identifier('я' * 128)
    check._check_quarantine_names('я' * 128, 'я' * 2048)
    check._check_resolution_reason('я' * 2048)
    check._check_consumer_id('я' * 2048)
    with pytest.raises(ValueError, match='task ID'):
        check._require_task_identifier('я' * 129)
    with pytest.raises(ValueError, match='consumer ID'):
        check._check_consumer_id('я' * 2049)
