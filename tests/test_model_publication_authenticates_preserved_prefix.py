"""Unchanged authenticated history is not newly authored provider output."""
import compile_memory as compiler
import markdown_transaction as transaction
import model_dlp
import pytest
from reliable_memory import canonical_json_bytes, sha256_bytes

from tests.adopted_vault import adopt

OLD = b'retained-private-fixture-value\n'
SAFE = b'new safe editorial observation\n'


def _policy(tmp_path, monkeypatch, literals=('retained-private-fixture-value', 'new-private-fixture-value', 'cross-boundary-secret')):
    payload = dict(version=1, literals=list(literals), allow_fingerprints=[])
    path = tmp_path / 'policy.json'
    path.write_bytes(canonical_json_bytes(dict(payload, sha256=sha256_bytes(canonical_json_bytes(payload)))))
    monkeypatch.setenv('LLM_WIKI_DLP_POLICY', str(path))
    return path


def _prepared(tmp_path, before, after, logical='knowledge/notes/retained.md'):
    root, state = adopt(tmp_path)
    target = root / logical
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(before)
    coordinator = transaction.active_markdown_coordinator(root, state)
    record = coordinator.prepare([transaction.MarkdownChange.replace(logical, after)], operation_id='compile:provenance-control', content_guard='model_output')
    return coordinator, record, target


def _blocked(coordinator, record, target, before):
    with pytest.raises(transaction.TransactionFailure):
        coordinator.apply(record.id)
    assert target.read_bytes() == before
    assert coordinator.transaction_state(record.id) == 'quarantined'


@pytest.mark.parametrize('logical', ['knowledge/notes/retained.md', 'knowledge/log.local.md'])
def test_safe_append_to_authenticated_existing_history_commits(tmp_path, monkeypatch, logical):
    _policy(tmp_path, monkeypatch)
    coordinator, record, target = _prepared(tmp_path, OLD, OLD + SAFE, logical)
    result = coordinator.apply(record.id)
    assert result.state == 'committed'
    assert target.read_bytes() == OLD + SAFE


@pytest.mark.parametrize('suffix', [b'new-private-fixture-value\n', OLD])
def test_new_or_repeated_existing_secret_is_still_blocked(tmp_path, monkeypatch, suffix):
    _policy(tmp_path, monkeypatch)
    coordinator, record, target = _prepared(tmp_path, OLD, OLD + suffix)
    _blocked(coordinator, record, target, OLD)


def test_secret_assembled_across_append_boundary_is_blocked(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch)
    before = OLD + b'cross-boundary-'
    coordinator, record, target = _prepared(tmp_path, before, before + b'secret')
    _blocked(coordinator, record, target, before)


def test_changed_old_prefix_cannot_claim_preserved_history(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch)
    coordinator, record, target = _prepared(tmp_path, OLD, b'changed prefix\n' + OLD + SAFE)
    _blocked(coordinator, record, target, OLD)


def test_create_has_no_authenticated_prefix_exemption(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch)
    root, state = adopt(tmp_path)
    coordinator = transaction.active_markdown_coordinator(root, state)
    logical = 'knowledge/notes/new.md'
    (root / 'knowledge/notes').mkdir(parents=True, exist_ok=True)
    record = coordinator.prepare([transaction.MarkdownChange.create(logical, OLD + SAFE)], operation_id='compile:create-provenance', content_guard='model_output')
    with pytest.raises(transaction.TransactionFailure):
        coordinator.apply(record.id)
    assert not (root / logical).exists()
    assert coordinator.transaction_state(record.id) == 'quarantined'


def test_required_policy_missing_at_publication_still_blocks(tmp_path, monkeypatch):
    policy = _policy(tmp_path, monkeypatch)
    coordinator, record, target = _prepared(tmp_path, OLD, OLD + SAFE)
    policy.unlink()
    _blocked(coordinator, record, target, OLD)


def test_live_before_hash_drift_cannot_be_preserved(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch)
    coordinator, record, target = _prepared(tmp_path, OLD, OLD + SAFE)
    changed = OLD + b'external edit\n'
    target.write_bytes(changed)
    with pytest.raises(transaction.TransactionFailure):
        coordinator.apply(record.id)
    assert target.read_bytes() == changed
    assert coordinator.transaction_state(record.id) == 'conflicted'


def test_materialized_before_bytes_must_match_canonical_hash(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch)
    coordinator, record, target = _prepared(tmp_path, OLD, OLD + SAFE)
    original = coordinator._materialized_state

    def changed_before(row, state):
        if 'length' in state:
            return OLD + b'wrong before proof\n'
        return original(row, state)

    monkeypatch.setattr(coordinator, '_materialized_state', changed_before)
    _blocked(coordinator, record, target, OLD)


@pytest.mark.parametrize('error_type', [RuntimeError, model_dlp.DLPContentBlocked])
def test_scanner_failure_never_becomes_preserved_history(tmp_path, monkeypatch, error_type):
    _policy(tmp_path, monkeypatch)
    coordinator, record, target = _prepared(tmp_path, OLD, OLD + SAFE)

    def fail_scan(*_args):
        raise error_type('scanner failed')

    monkeypatch.setattr(model_dlp, '_scrubbed', fail_scan)
    _blocked(coordinator, record, target, OLD)


def test_fresh_policy_after_prepare_blocks_new_delta(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch, literals=('retained-private-fixture-value',))
    suffix = b'newly protected fixture value\n'
    coordinator, record, target = _prepared(tmp_path, OLD, OLD + suffix)
    _policy(tmp_path, monkeypatch, literals=('retained-private-fixture-value', 'newly protected fixture value'))
    _blocked(coordinator, record, target, OLD)


def test_existing_whole_fingerprint_unlock_keeps_its_semantics(tmp_path, monkeypatch):
    after = OLD + b'new-private-fixture-value\n'
    payload = dict(version=1, literals=['retained-private-fixture-value', 'new-private-fixture-value'], allow_fingerprints=[sha256_bytes(after)])
    policy = tmp_path / 'policy.json'
    policy.write_bytes(canonical_json_bytes(dict(payload, sha256=sha256_bytes(canonical_json_bytes(payload)))))
    monkeypatch.setenv('LLM_WIKI_DLP_POLICY', str(policy))
    coordinator, record, target = _prepared(tmp_path, OLD, after)
    assert coordinator.apply(record.id).state == 'committed'
    assert target.read_bytes() == after


def test_safe_append_without_newline_keeps_exact_bytes(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch)
    before = OLD.rstrip(b'\n')
    after = before + b'; appended safe data'
    coordinator, record, target = _prepared(tmp_path, before, after)
    assert coordinator.apply(record.id).state == 'committed'
    assert target.read_bytes() == after


@pytest.mark.parametrize('before', [
    b'# History\nold entry  \n\n## Editorial note\nOperator-owned ending.\n',
    'История\r\n\r\n## Editorial note\r\nНеизменная запись. \t\r\n'.encode(),
    b'# History with no newline',
])
def test_compile_log_preserves_entire_old_prefix_byte_exactly(before):
    result = compiler._append_log_bytes(before, 'new compile observation  \n')
    assert result == before + b'new compile observation\n'


def test_actual_log_producer_and_publication_preserve_protected_editorial_history(tmp_path, monkeypatch):
    _policy(tmp_path, monkeypatch)
    before = b'# History\n' + OLD + b'\n## Editorial note\nOperator-owned ending.  \n'
    after = compiler._append_log_bytes(before, SAFE.decode())
    coordinator, record, target = _prepared(tmp_path, before, after, 'knowledge/log.local.md')
    assert coordinator.apply(record.id).state == 'committed'
    assert target.read_bytes() == before + SAFE


@pytest.mark.parametrize('error_type', [RuntimeError, model_dlp.DLPContentBlocked])
def test_scanner_failure_during_provenance_rescan_still_blocks(tmp_path, monkeypatch, error_type):
    _policy(tmp_path, monkeypatch)
    coordinator, record, target = _prepared(tmp_path, OLD, OLD + SAFE)
    original = model_dlp._scrubbed
    calls = []

    def fail_after_initial_scan(*args):
        calls.append(None)
        if len(calls) > 1:
            raise error_type('fallback scanner failed')
        return original(*args)

    monkeypatch.setattr(model_dlp, '_scrubbed', fail_after_initial_scan)
    _blocked(coordinator, record, target, OLD)
