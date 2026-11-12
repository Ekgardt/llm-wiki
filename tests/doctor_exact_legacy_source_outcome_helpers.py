"""Helpers shared by test_doctor_exact_legacy_source_outcome.py and the test files importing them."""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from pathlib import Path

import compile_pipeline
import pytest
from llm_wiki import compile_pipeline_receipts, doctor_transactions
from markdown_transaction import MarkdownChange, TransactionFailure, active_markdown_coordinator

from tests.adopted_vault import adopt
from tests.slow_machine import LONG_TIMEOUT


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _record(action):
    fixture = Path(__file__).parent / "fixtures/compile-receipt-v3-bag.json"
    raw = json.loads(fixture.read_text(encoding='utf-8'))["files"]["compile-receipt.md"]
    record = json.loads(raw.split("```json\n", 1)[1].split("\n```", 1)[0])
    record["action_key"] = _digest(action.encode())
    return _reidentify(record)


def _reidentify(record):
    manifest = record["batch_manifest"]
    record["source_identity"] = compile_pipeline.compile_source_identity(record["source"]["logical_path"], record["source"]["sha256"])
    record["batch_manifest_sha256"] = _digest(compile_pipeline.canonical_json_bytes(manifest))
    record["dispositions"] = sorted(
        [{"source_identity": compile_pipeline.compile_source_identity(source["logical_path"], source["sha256"]),
          "disposition": "no_durable_content"} for source in manifest],
        key=_disposition_key,
    )
    record["operation_id"] = compile_pipeline.compile_operation_id(record["action_key"], record["batch_manifest_sha256"], record["dispositions"])
    return record


def _disposition_key(item):
    return item["source_identity"]


def _bytes(record):
    return compile_pipeline_receipts.context_receipt_document(record).replace(b"compile-receipt/v4\n", b"compile-receipt/v3\n", 1)


def _receipt_path(record):
    return f"knowledge/daily/receipts/v3-{record['source_identity']}.md"


@pytest.fixture
def history(tmp_path):
    root, state = adopt(tmp_path)
    for relative in ("knowledge/notes", "knowledge/daily/receipts"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    old = _record("refused")
    daily = root / old["source"]["logical_path"]
    fixture = json.loads((Path(__file__).parent / "fixtures/compile-receipt-v3-bag.json").read_text(encoding='utf-8'))
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


@contextmanager
def _reader(history, *, deadline=None):
    root, state, coordinator, _refused, _old = history
    stop = time.monotonic() + LONG_TIMEOUT if deadline is None else deadline
    with coordinator._authority_read_connection(stop) as database:
        yield database, doctor_transactions.CompiledDaySupersession(root, state, database=database, deadline=stop)


def _resolves(history):
    with _reader(history) as (database, supersession):
        return supersession.resolves(database, history[3].id, set())
