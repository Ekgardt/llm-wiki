"""Current v4 work may prove exact historical source bytes, never old offsets."""
import copy
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import compile_memory as compiler
import doctor
import markdown_transaction
import pytest
from markdown_transaction import MarkdownChange, TransactionFailure, active_markdown_coordinator
from transaction_lineage import quarantine_witnesses

from tests.adopted_vault import adopt
from tests.test_doctor_exact_legacy_source_outcome import (
    _bytes,
    _digest,
    _reader,
    _receipt_path,
    _record,
    _reidentify,
    _resolves,
)
from tests.test_native_compile_companion_adopted import (
    _adopted_environment,
    _committed_companion,
    _pending_batch,
)
from tests.test_native_compile_uses_whole_container import (
    _apply_controlled_batch,
    _frame_covering_keys,
)
from tests.test_native_user_frames_keep_their_physical_evidence import _captured_vault, _frame


def _current_record(old, part):
    record = copy.deepcopy(old)
    record["schema_version"] = "compile-receipt/v4"
    record["source"] = compiler._v4_source_descriptor(part)
    record["batch_manifest"] = [copy.deepcopy(record["source"])]
    record["source_identity"] = compiler.compile_context_source_identity(record["source"])
    record["batch_manifest_sha256"] = _digest(compiler.canonical_json_bytes(record["batch_manifest"]))
    record["dispositions"] = [{"source_identity": record["source_identity"], "disposition": "no_durable_content"}]
    record["action_key"] = _digest(b"current-v4-execution")
    record["operation_id"] = compiler._compile_operation_id(record["action_key"], record["batch_manifest_sha256"], record["dispositions"])
    return record


@pytest.fixture
def exact_work(tmp_path, monkeypatch):
    root, state = adopt(tmp_path)
    for relative in ("knowledge/notes", "knowledge/daily/receipts"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    old = _record("old-quarantined")
    logical = old["source"]["logical_path"]
    whole = b"# Daily\n\n## [00:00:00] First\n" + b"a" * 12000 + b"\n## [01:00:00] Next\n" + b"b" * 12000 + b"\n"
    (root / logical).write_bytes(whole)
    monkeypatch.setattr(compiler, "ROOT", root)
    part = compiler._daily_parts(logical, whole)[0]
    old["source"] = compiler._source_descriptor(part).receipt_descriptor()
    old["batch_manifest"] = [copy.deepcopy(old["source"])]
    _reidentify(old)
    coordinator = active_markdown_coordinator(root, state)
    refused = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/never-published.md", b"# Old\n"),
         MarkdownChange.create(_receipt_path(old), _bytes(old))],
        operation_id=old["operation_id"], preconditions={"knowledge/notes/missing.md": "0" * 64},
    )
    with pytest.raises(TransactionFailure):
        coordinator.apply(refused.id)
    return (root, state, coordinator, refused, old), part


def _publish_current(history, record, *, apply=True):
    path = f"knowledge/daily/receipts/v4-{record['source_identity']}.md"
    transaction = history[2].prepare(
        [MarkdownChange.create(path, compiler._context_receipt_document(record))],
        operation_id=record["operation_id"],
    )
    if apply:
        history[2].apply(transaction.id)
    return transaction


def test_committed_v4_proves_exact_old_selected_bytes(exact_work):
    history, part = exact_work
    _publish_current(history, _current_record(history[4], part))
    assert _resolves(history)
    assert history[2].transaction_state(history[3].id) == "quarantined"
    assert not (history[0] / "knowledge/notes/never-published.md").exists()


def test_missing_current_v4_execution_stays_unresolved(exact_work):
    history, _part = exact_work
    assert not _resolves(history)


def test_prepared_v4_is_not_execution_authority(exact_work):
    history, part = exact_work
    record = _current_record(history[4], part)
    _publish_current(history, record, apply=False)
    path = history[0] / f"knowledge/daily/receipts/v4-{record['source_identity']}.md"
    path.write_bytes(compiler._context_receipt_document(record))
    assert not _resolves(history)


@pytest.mark.parametrize("damage", ["append", "replace-selected", "duplicate-selected"])
def test_changed_or_ambiguous_current_source_is_unresolved(exact_work, damage):
    history, part = exact_work
    _publish_current(history, _current_record(history[4], part))
    path = history[0] / part.logical_path
    changes = {
        "append": path.read_bytes() + b"\n## [02:00:00] New\nchanged\n",
        "replace-selected": path.read_bytes().replace(b"a" * 100, b"c" * 100, 1),
        "duplicate-selected": path.read_bytes() + part.content,
    }
    path.write_bytes(changes[damage])
    assert not _resolves(history)


def _compiled_current_record(old, part):
    record = _current_record(old, part)
    note = MarkdownChange.create("knowledge/notes/current-output.md", b"# Current interpretation\n")
    record["operations"] = [{"kind": "create", "path": note.path, "after_sha256": _digest(note.content)}]
    record["evidence"] = [{"source_identity": record["source_identity"], "operation_path": note.path,
                           "source_path": part.logical_path, "source_digest": part.sha256,
                           "quote_sha256": _digest(b"a" * 100)}]
    record["dispositions"][0]["disposition"] = "compiled"
    record["operation_id"] = compiler._compile_operation_id(record["action_key"], record["batch_manifest_sha256"], record["dispositions"])
    return record, note


def _publish_with_output(history, record, note):
    path = f"knowledge/daily/receipts/v4-{record['source_identity']}.md"
    transaction = history[2].prepare(
        [note, MarkdownChange.create(path, compiler._context_receipt_document(record))],
        operation_id=record["operation_id"],
    )
    history[2].apply(transaction.id)
    return transaction


def test_compiled_current_outcome_requires_same_transaction_live_output(exact_work):
    history, part = exact_work
    record, note = _compiled_current_record(history[4], part)
    _publish_with_output(history, record, note)
    _assert_current_work(history)
    assert _resolves(history)
    (history[0] / note.path).write_bytes(b"# Unrelated later edit\n")
    assert not _resolves(history)


def test_receipt_and_output_from_different_transactions_are_unresolved(exact_work):
    history, part = exact_work
    record, note = _compiled_current_record(history[4], part)
    created = history[2].prepare([note], operation_id="other-output")
    history[2].apply(created.id)
    _publish_current(history, record)
    assert not _resolves(history)


def test_replace_only_current_receipt_is_not_new_execution(exact_work):
    history, part = exact_work
    record = _current_record(history[4], part)
    path = f"knowledge/daily/receipts/v4-{record['source_identity']}.md"
    (history[0] / path).write_bytes(b"# External placeholder\n")
    transaction = history[2].prepare(
        [MarkdownChange.replace(path, compiler._context_receipt_document(record))], operation_id=record["operation_id"],
    )
    history[2].apply(transaction.id)
    assert not _resolves(history)


def _refuse_native_history(root, coordinator, unit):
    old = _record("old-native-packet")
    raw = b"".join(part.content for part in unit)
    selected = replace(unit[0], content=raw, sha256=_digest(raw))
    old["source"] = compiler._source_descriptor(selected).receipt_descriptor()
    old["batch_manifest"] = [copy.deepcopy(old["source"])]
    _reidentify(old)
    refused = coordinator.prepare(
        [MarkdownChange.create("knowledge/notes/never-native.md", b"# Old\n"),
         MarkdownChange.create(_receipt_path(old), _bytes(old))],
        operation_id=old["operation_id"], preconditions={"knowledge/notes/missing.md": "0" * 64},
    )
    with pytest.raises(TransactionFailure):
        coordinator.apply(refused.id)
    return root, coordinator.state_root, coordinator, refused, old


@pytest.fixture
def native_work(tmp_path, monkeypatch):
    root, coordinator, first, _old, path, _content = _committed_companion(tmp_path, monkeypatch)
    batch, plan = _pending_batch(root, coordinator, first)
    result = _apply_controlled_batch(batch, plan, coordinator, "b" * 64)
    assert result.state == "committed"
    history = _refuse_native_history(root, coordinator, batch.inputs.dailies)
    return history, batch, path


def test_complete_native_work_recognizes_preserved_companion(native_work):
    history, _batch, _path = native_work
    _assert_current_work(history)
    assert _resolves(history)
    assert history[2].transaction_state(history[3].id) == "quarantined"


def test_missing_native_companion_remains_unresolved(native_work):
    history, _batch, path = native_work
    path.unlink()
    assert not _resolves(history)


def test_partial_historical_native_packet_is_not_complete_work(native_work):
    history, batch, _path = native_work
    partial = _refuse_native_history(history[0], history[2], [batch.inputs.dailies[0]])
    assert not _resolves(partial)


def _assert_current_work(history):
    with _reader(history) as (_database, reader):
        assert doctor._CurrentHistoricalSourceWork(reader, history[4]).verified()


def test_actual_deadline_expiry_remains_visible(exact_work):
    history, part = exact_work
    _publish_current(history, _current_record(history[4], part))
    with _reader(history) as (database, reader):
        reader.deadline = time.monotonic()
        with pytest.raises(TimeoutError):
            reader.resolves(database, history[3].id, set())


def _reidentify_v4(record):
    record["batch_manifest_sha256"] = _digest(compiler.canonical_json_bytes(record["batch_manifest"]))
    record["dispositions"] = sorted(
        [{"source_identity": compiler.compile_context_source_identity(source), "disposition": "no_durable_content"}
         for source in record["batch_manifest"]], key=_source_identity,
    )
    record["operation_id"] = compiler._compile_operation_id(record["action_key"], record["batch_manifest_sha256"], record["dispositions"])
    return record


def _source_identity(item):
    return item["source_identity"]


def test_different_current_batch_does_not_prove_old_complete_batch(exact_work):
    history, part = exact_work
    record = _current_record(history[4], part)
    other = compiler._daily_parts(part.logical_path, part.original_content)[1]
    record["batch_manifest"].append(compiler._v4_source_descriptor(other))
    record["packing"]["algorithm"] = "compile-complete-items/v2"
    _publish_current(history, _reidentify_v4(record))
    assert not _resolves(history)


@pytest.mark.parametrize("disposition", ["pending", "failed", "unknown"])
def test_nonterminal_current_disposition_is_not_source_work(exact_work, disposition):
    history, part = exact_work
    record = _current_record(history[4], part)
    record["dispositions"][0]["disposition"] = disposition
    _publish_current(history, record)
    assert not _resolves(history)


def test_wrong_current_operation_identity_remains_unresolved(exact_work):
    history, part = exact_work
    record = _current_record(history[4], part)
    path = f"knowledge/daily/receipts/v4-{record['source_identity']}.md"
    transaction = history[2].prepare(
        [MarkdownChange.create(path, compiler._context_receipt_document(record))], operation_id="other-operation",
    )
    history[2].apply(transaction.id)
    assert not _resolves(history)


def test_native_permanent_head_tamper_remains_unresolved(native_work):
    history, _batch, _path = native_work
    heads = list((history[0] / "knowledge/raw/sessions").rglob("*.breadcrumb.md"))
    assert len(heads) == 1
    heads[0].write_bytes(heads[0].read_bytes() + b"external corruption\n")
    assert not _resolves(history)


@pytest.fixture
def tool_work(tmp_path, monkeypatch):
    root, coordinator = _adopted_environment(tmp_path, monkeypatch)
    _captured_vault(root, _frame("", event_type="post_tool_use", payload={"tool_name": "read", "target": "file", "data": "A" * 22000}))
    path = root / "knowledge/daily/2026-09-29.md"
    inputs = compiler.snapshot_compile_inputs([path])
    keys = _frame_covering_keys(inputs, path.read_bytes())
    selected = compiler._subset_compile_inputs(inputs, keys)
    batch = compiler.pack_compile_batches(selected, model=None)[0]
    plan = compiler._normalize_plan([], batch.inputs)
    result = _apply_controlled_batch(batch, plan, coordinator, "c" * 64)
    assert result.state == "committed"
    history = _refuse_native_history(root, coordinator, batch.inputs.dailies)
    return history, batch


def test_complete_tool_physical_line_is_current_work(tool_work):
    history, _batch = tool_work
    _assert_current_work(history)
    assert _resolves(history)


def test_tool_companion_cannot_be_omitted(tool_work):
    history, batch = tool_work
    identity = compiler.compile_context_source_identity(compiler._v4_source_descriptor(batch.inputs.dailies[0]))
    (history[0] / f"knowledge/daily/receipts/v4-{identity}.md").unlink()
    assert not _resolves(history)


@pytest.mark.parametrize("target", ["source", "output", "receipt"])
def test_changes_after_initial_witness_are_refused(exact_work, monkeypatch, target):
    history, part = exact_work
    record, note = _compiled_current_record(history[4], part)
    _publish_with_output(history, record, note)
    paths = {"source": part.logical_path, "output": note.path,
             "receipt": f"knowledge/daily/receipts/v4-{record['source_identity']}.md"}
    original = doctor._CurrentHistoricalSourceWork._witness
    changed = []

    def change_after_witness(work, witness):
        result = original(work, witness)
        if not changed:
            path = history[0] / paths[target]
            path.write_bytes(path.read_bytes() + b"external change\n")
            changed.append(True)
        return result

    monkeypatch.setattr(doctor._CurrentHistoricalSourceWork, "_witness", change_after_witness)
    assert not _resolves(history)


def test_existing_compile_family_retains_current_v4_authority_after_pruning(exact_work):
    history, part = exact_work
    record = _current_record(history[4], part)
    committed = _publish_current(history, record)
    with _reader(history) as (database, _reader_instance):
        assert committed.id not in quarantine_witnesses(database)
    now = datetime.now(timezone.utc)
    assert history[2].prune(now=now + timedelta(days=3)) == 1
    removed = history[2].prune_history(now=now + timedelta(days=markdown_transaction.HISTORY_RETENTION_DAYS + 1))
    assert removed["transactions"] == 0
    assert history[2].committed_attempt(record["operation_id"]).id == committed.id
    assert _resolves(history)
    assert history[2].transaction_state(history[3].id) == "quarantined"


def _two_source_history(exact_work):
    history, first = exact_work
    parts = compiler._daily_parts(first.logical_path, first.original_content)
    old = copy.deepcopy(history[4])
    old["action_key"] = _digest(b"old-two-source-batch")
    old["batch_manifest"] = [compiler._source_descriptor(part).receipt_descriptor() for part in parts]
    _reidentify(old)
    refused = history[2].prepare(
        [MarkdownChange.create("knowledge/notes/two-old-unpublished.md", b"# Old two-source batch\n"),
         MarkdownChange.create(_receipt_path(old), _bytes(old))],
        operation_id=old["operation_id"], preconditions={"knowledge/notes/missing.md": "0" * 64},
    )
    with pytest.raises(TransactionFailure):
        history[2].apply(refused.id)
    return (*history[:3], refused, old), parts


def _two_current_records(old, parts):
    records = [_current_record(old, part) for part in parts]
    manifest = [compiler._v4_source_descriptor(part) for part in parts]
    for record in records:
        record["batch_manifest"] = manifest
        record["packing"]["algorithm"] = "compile-complete-items/v2"
        _reidentify_v4(record)
    return records


def _publish_multi_current(history, records):
    changes = [MarkdownChange.create(f"knowledge/daily/receipts/v4-{record['source_identity']}.md",
                                     compiler._context_receipt_document(record)) for record in records]
    transaction = history[2].prepare(changes, operation_id=records[0]["operation_id"])
    history[2].apply(transaction.id)


def test_two_old_units_at_one_path_need_one_complete_current_batch(exact_work):
    history, parts = _two_source_history(exact_work)
    _publish_multi_current(history, _two_current_records(history[4], parts))
    _assert_current_work(history)
    assert _resolves(history)


@pytest.mark.parametrize("damage", ["append", "change"])
def test_mixed_physical_versions_between_source_captures_are_refused(exact_work, monkeypatch, damage):
    history, parts = _two_source_history(exact_work)
    _publish_multi_current(history, _two_current_records(history[4], parts))
    original = doctor._CurrentHistoricalSourceWork._capture
    captures = []

    def capture_then_change(work, logical):
        selected = original(work, logical)
        if not captures:
            path = history[0] / logical
            changes = {"append": path.read_bytes() + b"\n## [02:00:00] New\nchanged\n",
                       "change": path.read_bytes().replace(b"a" * 100, b"c" * 100, 1)}
            path.write_bytes(changes[damage])
        captures.append(logical)
        return selected

    monkeypatch.setattr(doctor._CurrentHistoricalSourceWork, "_capture", capture_then_change)
    assert not _resolves(history)
    assert len(captures) == 2


def test_same_digest_at_a_different_current_path_is_unresolved(exact_work):
    history, part = exact_work
    other = replace(part, logical_path="knowledge/daily/2026-01-02.md")
    (history[0] / other.logical_path).write_bytes(part.original_content)
    _publish_current(history, _current_record(history[4], other))
    assert not _resolves(history)


def test_partial_ordinary_historical_packet_is_not_a_complete_unit(exact_work):
    history, part = exact_work
    raw = part.content[:-1]
    selected = replace(part, content=raw, sha256=_digest(raw))
    _publish_current(history, _current_record(history[4], part))
    partial = _refuse_native_history(history[0], history[2], [selected])
    assert not _resolves(partial)
