"""Helpers shared by test_doctor_current_v4_exact_historical_work.py and the test files importing them."""

import copy
from dataclasses import replace

import compile_pipeline
import pytest
from llm_wiki import compile_pipeline_receipts, compile_pipeline_sources
from markdown_transaction import MarkdownChange, TransactionFailure, active_markdown_coordinator

from tests.adopted_vault import adopt
from tests.doctor_exact_legacy_source_outcome_helpers import (
    _bytes,
    _digest,
    _receipt_path,
    _record,
    _reidentify,
)
from tests.native_compile_companion_adopted_helpers import _committed_companion, _pending_batch
from tests.native_compile_uses_whole_container_helpers import _apply_controlled_batch


def _current_record(old, part):
    record = copy.deepcopy(old)
    record["schema_version"] = "compile-receipt/v4"
    record["source"] = compile_pipeline_receipts.v4_source_descriptor(part)
    record["batch_manifest"] = [copy.deepcopy(record["source"])]
    record["source_identity"] = compile_pipeline.compile_context_source_identity(record["source"])
    record["batch_manifest_sha256"] = _digest(compile_pipeline.canonical_json_bytes(record["batch_manifest"]))
    record["dispositions"] = [{"source_identity": record["source_identity"], "disposition": "no_durable_content"}]
    record["action_key"] = _digest(b"current-v4-execution")
    record["operation_id"] = compile_pipeline.compile_operation_id(record["action_key"], record["batch_manifest_sha256"], record["dispositions"])
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
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    part = compile_pipeline_sources.daily_parts(logical, whole)[0]
    old["source"] = compile_pipeline_sources.source_descriptor(part).receipt_descriptor()
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


def _refuse_native_history(root, coordinator, unit):
    old = _record("old-native-packet")
    raw = b"".join(part.content for part in unit)
    selected = replace(unit[0], content=raw, sha256=_digest(raw))
    old["source"] = compile_pipeline_sources.source_descriptor(selected).receipt_descriptor()
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


def _reidentify_v4(record):
    record["batch_manifest_sha256"] = _digest(compile_pipeline.canonical_json_bytes(record["batch_manifest"]))
    record["dispositions"] = sorted(
        [{"source_identity": compile_pipeline.compile_context_source_identity(source), "disposition": "no_durable_content"}
         for source in record["batch_manifest"]], key=_source_identity,
    )
    record["operation_id"] = compile_pipeline.compile_operation_id(record["action_key"], record["batch_manifest_sha256"], record["dispositions"])
    return record


def _source_identity(item):
    return item["source_identity"]


def _two_source_history(exact_work):
    history, first = exact_work
    parts = compile_pipeline_sources.daily_parts(first.logical_path, first.original_content)
    old = copy.deepcopy(history[4])
    old["action_key"] = _digest(b"old-two-source-batch")
    old["batch_manifest"] = [compile_pipeline_sources.source_descriptor(part).receipt_descriptor() for part in parts]
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
    manifest = [compile_pipeline_receipts.v4_source_descriptor(part) for part in parts]
    for record in records:
        record["batch_manifest"] = manifest
        record["packing"]["algorithm"] = "compile-complete-items/v2"
        _reidentify_v4(record)
    return records


def _publish_multi_current(history, records):
    changes = [MarkdownChange.create(f"knowledge/daily/receipts/v4-{record['source_identity']}.md",
                                     compile_pipeline_receipts.context_receipt_document(record)) for record in records]
    transaction = history[2].prepare(changes, operation_id=records[0]["operation_id"])
    history[2].apply(transaction.id)
