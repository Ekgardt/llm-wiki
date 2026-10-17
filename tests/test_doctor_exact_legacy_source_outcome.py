"""A historical source outcome needs complete committed receipt authority."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import compile_memory as compiler
import doctor
import pytest
from markdown_transaction import MarkdownChange, TransactionFailure, active_markdown_coordinator
from transaction_lineage import quarantine_witnesses

from tests.adopted_vault import adopt
from tests.slow_machine import LONG_TIMEOUT


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _record(action):
    fixture = Path(__file__).parent / "fixtures/compile-receipt-v3-bag.json"
    raw = json.loads(fixture.read_text())["files"]["compile-receipt.md"]
    record = json.loads(raw.split("```json\n", 1)[1].split("\n```", 1)[0])
    record["action_key"] = _digest(action.encode())
    return _reidentify(record)


def _reidentify(record):
    manifest = record["batch_manifest"]
    record["source_identity"] = compiler.compile_source_identity(record["source"]["logical_path"], record["source"]["sha256"])
    record["batch_manifest_sha256"] = _digest(compiler.canonical_json_bytes(manifest))
    record["dispositions"] = sorted(
        [{"source_identity": compiler.compile_source_identity(source["logical_path"], source["sha256"]),
          "disposition": "no_durable_content"} for source in manifest],
        key=_disposition_key,
    )
    record["operation_id"] = compiler._compile_operation_id(record["action_key"], record["batch_manifest_sha256"], record["dispositions"])
    return record


def _disposition_key(item):
    return item["source_identity"]


def _bytes(record):
    return compiler._context_receipt_document(record).replace(b"compile-receipt/v4\n", b"compile-receipt/v3\n", 1)


def _receipt_path(record):
    return f"knowledge/daily/receipts/v3-{record['source_identity']}.md"


@pytest.fixture
def history(tmp_path):
    root, state = adopt(tmp_path)
    for relative in ("knowledge/notes", "knowledge/daily/receipts"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    old = _record("refused")
    daily = root / old["source"]["logical_path"]
    fixture = json.loads((Path(__file__).parent / "fixtures/compile-receipt-v3-bag.json").read_text())
    original = fixture["files"]["data/2026-01-01.md"].encode()
    assert _digest(original) == old["source"]["sha256"]
    daily.write_bytes(original + b"\n# Current whole day has new content\n")
    coordinator = active_markdown_coordinator(root, state)
    refused = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/old-unpublished.md", b"# Old interpretation\n"),
         MarkdownChange.create(_receipt_path(old), _bytes(old))],
        operation_id=old["operation_id"],
        preconditions={"knowledge/notes/missing.md": "0" * 64},
    )
    with pytest.raises(TransactionFailure):
        coordinator.apply(refused.id)
    return root, state, coordinator, refused, old


def _publish(history, record, *, apply=True, extra=()):
    _root, _state, coordinator, _refused, _old = history
    transaction = coordinator.prepare(
        [*extra, MarkdownChange.create(_receipt_path(record), _bytes(record))],
        operation_id=record["operation_id"],
    )
    if apply:
        coordinator.apply(transaction.id)
    return transaction


@contextmanager
def _reader(history, *, deadline=None):
    root, state, coordinator, _refused, _old = history
    stop = time.monotonic() + LONG_TIMEOUT if deadline is None else deadline
    with coordinator._authority_read_connection(stop) as database:
        yield database, doctor._CompiledDaySupersession(root, state, database=database, deadline=stop)


def _resolves(history):
    with _reader(history) as (database, supersession):
        return supersession.resolves(database, history[3].id, set())


def test_different_committed_receipt_proves_exact_historical_source(history):
    current = _record("different-successful-attempt")
    committed = _publish(history, current)
    assert current["operation_id"] != history[4]["operation_id"]
    assert _bytes(current) != _bytes(history[4])
    assert _resolves(history)
    assert history[2].transaction_state(history[3].id) == "quarantined"
    assert not (history[0] / "knowledge/notes/old-unpublished.md").exists()
    with _reader(history) as (database, _supersession):
        assert committed.id in quarantine_witnesses(database)


def test_historical_outcome_does_not_authorize_current_day_or_deletion(history):
    _publish(history, _record("success"))
    with _reader(history) as (_database, supersession):
        assert not supersession._whole_legacy_source_compiled(history[4]["source"])
    details, states = doctor._empty_transaction_details()
    states["quarantined"] = 1
    details["quarantined_unresolved"] = 0
    result = doctor._transaction_result(details, states)
    assert result["details"]["states"]["quarantined"] == 1
    assert "transaction_quarantined" in result["details"]["deletion_codes"]


@pytest.mark.parametrize("field,value", [
    ("byte_size", 73),
    ("occurrence_bounds", {"first_event_id": "one", "last_event_id": "two"}),
])
def test_same_digest_with_different_selected_descriptor_is_unresolved(history, field, value):
    record = _record("other-descriptor")
    record["source"][field] = value
    record["batch_manifest"] = [copy.deepcopy(record["source"])]
    _publish(history, _reidentify(record))
    assert not _resolves(history)


def test_same_digest_at_another_logical_path_is_unresolved(history):
    record = _record("other-path")
    record["source"]["logical_path"] = "knowledge/daily/2026-01-02.md"
    record["batch_manifest"] = [copy.deepcopy(record["source"])]
    _publish(history, _reidentify(record))
    assert not _resolves(history)


def test_another_batch_with_the_same_source_is_unresolved(history):
    record = _record("other-batch")
    extra = dict(record["source"], logical_path="knowledge/daily/2026-01-02.md")
    record["batch_manifest"].append(extra)
    _publish(history, _reidentify(record))
    assert not _resolves(history)


def test_missing_current_receipt_is_explicitly_unresolved(history):
    assert not _resolves(history)


def test_uncommitted_receipt_bytes_cannot_prove_a_terminal_outcome(history):
    record = _record("prepared-only")
    _publish(history, record, apply=False)
    (history[0] / _receipt_path(record)).write_bytes(_bytes(record))
    assert not _resolves(history)


@pytest.mark.parametrize("damage", ["unknown-version", "nonterminal", "noncanonical", "corrupt"])
def test_invalid_committed_receipt_is_unresolved(history, damage):
    record = _record("invalid-receipt")
    raw = _damaged_bytes(record, damage)
    transaction = history[2].prepare([MarkdownChange.create(_receipt_path(record), raw)], operation_id=record["operation_id"])
    history[2].apply(transaction.id)
    assert not _resolves(history)


def _damaged_bytes(record, damage):
    replacements = {
        "unknown-version": (b"compile-receipt/v3", b"compile-receipt/v999"),
        "nonterminal": (b"no_durable_content", b"pending"),
        "noncanonical": (b'"action_key":', b'"action_key" :'),
        "corrupt": (b"```json", b"```broken"),
    }
    before, after = replacements[damage]
    return _bytes(record).replace(before, after)


def test_modified_staged_receipt_image_is_unresolved(history):
    _publish(history, _record("success"))
    image = history[1] / "run/transactions" / history[3].id / "after/000001.bin"
    image.write_bytes(b"tampered")
    assert not _resolves(history)


def test_changed_canonical_operation_hash_is_unresolved(history):
    committed = _publish(history, _record("success"))
    with sqlite3.connect(history[2].database_path) as database:
        database.execute("UPDATE operation SET after_hash=? WHERE transaction_id=?", ("0" * 64, committed.id))
    assert not _resolves(history)


def test_a_replaced_receipt_without_a_retained_create_witness_is_unresolved(history):
    record = _record("replace-only")
    path = _receipt_path(record)
    before = b"# External preexisting receipt placeholder\n"
    (history[0] / path).write_bytes(before)
    transaction = history[2].prepare(
        [MarkdownChange.replace(path, _bytes(record))], operation_id=record["operation_id"],
    )
    history[2].apply(transaction.id)
    assert not _resolves(history)


def test_a_staged_receipt_bound_to_another_request_is_unresolved(history):
    _publish(history, _record("success"))
    with sqlite3.connect(history[2].database_path) as database:
        database.execute('UPDATE "transaction" SET operation_id=? WHERE id=?', ("compile:" + "a" * 64, history[3].id))
    assert not _resolves(history)


def test_current_receipt_tamper_after_commit_is_unresolved(history):
    record = _record("success")
    _publish(history, record)
    (history[0] / _receipt_path(record)).write_bytes(_bytes(record) + b"\n")
    assert not _resolves(history)


def _compiled_record():
    record = _record("compiled-success")
    note = b"# A different verified interpretation\n"
    path = "knowledge/notes/new-interpretation.md"
    record["operations"] = [{"kind": "create", "path": path, "after_sha256": _digest(note)}]
    record["evidence"] = [{"source_identity": record["source_identity"], "operation_path": path,
                           "source_path": record["source"]["logical_path"],
                           "source_digest": record["source"]["sha256"], "quote_sha256": _digest(b"Manual source")}]
    record["dispositions"][0]["disposition"] = "compiled"
    record["operation_id"] = compiler._compile_operation_id(record["action_key"], record["batch_manifest_sha256"], record["dispositions"])
    return record, MarkdownChange.create(path, note)


def test_compiled_outcome_requires_real_declared_output_operation(history):
    record, note = _compiled_record()
    _publish(history, record, extra=(note,))
    assert _resolves(history)
    assert not (history[0] / "knowledge/notes/old-unpublished.md").exists()


def test_missing_declared_output_operation_is_unresolved(history):
    record, _note = _compiled_record()
    _publish(history, record)
    assert not _resolves(history)


def test_changed_declared_output_hash_is_unresolved(history):
    record, note = _compiled_record()
    committed = _publish(history, record, extra=(note,))
    with sqlite3.connect(history[2].database_path) as database:
        database.execute("UPDATE operation SET after_hash=? WHERE transaction_id=? AND path=?",
                         ("0" * 64, committed.id, note.path))
    assert not _resolves(history)


def test_create_and_output_proofs_cannot_come_from_different_transactions(history):
    record, note = _compiled_record()
    _publish(history, record)
    replaced = history[2].prepare(
        [note, MarkdownChange.replace(_receipt_path(record), _bytes(record))],
        operation_id=record["operation_id"] + "#1",
    )
    history[2].apply(replaced.id)
    with _reader(history) as (database, _supersession):
        assert replaced.id not in quarantine_witnesses(database)
    assert not _resolves(history)


def test_receipt_changed_during_authority_check_is_unresolved(history, monkeypatch):
    record = _record("success")
    _publish(history, record)
    original = doctor._committed_snapshot_receipt

    def check(*args, **kwargs):
        proved = original(*args, **kwargs)
        (history[0] / _receipt_path(record)).write_bytes(_bytes(record) + b"\n")
        return proved

    monkeypatch.setattr(doctor, "_committed_snapshot_receipt", check)
    assert not _resolves(history)


def _receipt_for_source(record, source):
    selected = copy.deepcopy(record)
    selected["source"] = copy.deepcopy(source)
    selected["source_identity"] = compiler.compile_source_identity(source["logical_path"], source["sha256"])
    return selected


def _two_source_history(history):
    old = _record("two-source-refusal")
    old["batch_manifest"].append(dict(old["source"], logical_path="knowledge/daily/2026-01-02.md"))
    _reidentify(old)
    changes = [MarkdownChange.create(_receipt_path(record), _bytes(record))
               for record in [_receipt_for_source(old, source) for source in old["batch_manifest"]]]
    changes.append(MarkdownChange.create("knowledge/notes/other-old-unpublished.md", b"# Other old page\n"))
    refused = history[2].prepare(changes, operation_id=old["operation_id"],
                                 preconditions={"knowledge/notes/missing.md": "0" * 64})
    with pytest.raises(TransactionFailure):
        history[2].apply(refused.id)
    return *history[:3], refused, old


def _two_source_success(history):
    record = copy.deepcopy(history[4])
    record["action_key"] = _digest(b"two-source-success")
    return _reidentify(record)


def test_every_staged_receipt_in_the_batch_needs_a_committed_outcome(history):
    multiple = _two_source_history(history)
    _publish(multiple, _two_source_success(multiple))
    assert not _resolves(multiple)


def test_all_receipts_with_the_exact_complete_manifest_prove_history(history):
    multiple = _two_source_history(history)
    first = _two_source_success(multiple)
    second = _receipt_for_source(first, first["batch_manifest"][1])
    _publish(multiple, first, extra=(MarkdownChange.create(_receipt_path(second), _bytes(second)),))
    assert _resolves(multiple)
    assert multiple[2].transaction_state(multiple[3].id) == "quarantined"


def test_exact_receipt_without_its_own_source_in_the_batch_is_unresolved(history):
    old = _record("unbound-old-source")
    old["batch_manifest"] = [dict(old["source"], logical_path="knowledge/daily/2026-01-02.md")]
    _reidentify(old)
    refused = history[2].prepare(
        [MarkdownChange.create(_receipt_path(old), _bytes(old)),
         MarkdownChange.create("knowledge/notes/unbound-old.md", b"# Old\n")],
        operation_id=old["operation_id"], preconditions={"knowledge/notes/missing.md": "0" * 64},
    )
    with pytest.raises(TransactionFailure):
        history[2].apply(refused.id)
    unbound = (*history[:3], refused, old)
    current = copy.deepcopy(old)
    current["action_key"] = _digest(b"unbound-current-source")
    _publish(unbound, _reidentify(current))
    assert not _resolves(unbound)


def test_a_current_v4_receipt_cannot_upgrade_a_staged_v3_source(history):
    record = _record("wrong-version")
    raw = _bytes(record).replace(b"compile-receipt/v3", b"compile-receipt/v4")
    transaction = history[2].prepare([MarkdownChange.create(_receipt_path(record), raw)], operation_id=record["operation_id"])
    history[2].apply(transaction.id)
    assert not _resolves(history)


def test_expired_deadline_is_not_a_historical_success(history):
    _publish(history, _record("success"))
    with pytest.raises(TimeoutError):
        with _reader(history, deadline=time.monotonic() - 1) as (database, supersession):
            supersession.resolves(database, history[3].id, set())
