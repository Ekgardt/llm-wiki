"""Helpers shared by test_a_continuation_keeps_its_original_entry.py and the test files importing them."""
from __future__ import annotations

import json

import compile_receipts
from evidence_resolver import EvidenceResolver
from llm_wiki import (
    compile_pipeline_apply,
    compile_pipeline_batches,
    compile_pipeline_operations,
    compile_pipeline_receipts,
    compile_pipeline_sources,
)
from markdown_transaction import MarkdownCoordinator
from reliable_memory import canonical_json_bytes

from tests.compile_transactions_helpers import _semantic_plan
from tests.compile_transactions_helpers import vault as vault


def _continuation():
    prefix = b"## [10:00:00] session-end | manual\n" + b"An earlier observation.\n" * 900
    quote = "The continuation preserves the original observation clock."
    content = prefix + quote.encode() + b"\n"
    parts = compile_pipeline_sources.daily_parts("knowledge/daily/2026-10-03.md", content)
    selected = next(part for part in parts if quote.encode() in part.content)
    inputs = compile_pipeline_sources.CompileInputs(
        (selected,),
        (compile_receipts.SourceSnapshot(selected.logical_path, selected.content, selected.sha256),),
        (),
    )
    evidence = {
        "daily_date": "2026-10-03",
        "timestamp": "10:00:00",
        "quoted_text": quote,
        "claim": "Continuation evidence retains its clock.",
    }
    return content, selected, inputs, evidence


def _published_continuation(vault):
    root, state_root = vault
    content, _selected, _inputs, evidence = _continuation()
    path = root / "knowledge/daily/2026-07-14.md"
    path.write_bytes(content)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([path])
    batches = compile_pipeline_batches.pack_compile_batches(inputs, model="fake-v1")
    batch = next(item for item in batches if evidence["quoted_text"].encode() in item.inputs.dailies[0].content)
    plan = _semantic_plan()
    operation = json.loads(plan["operations"][0]["content"])
    operation["evidence"][0]["quoted_text"] = evidence["quoted_text"]
    operation["body_markdown"] = evidence["quoted_text"]
    plan["operations"][0]["content"] = canonical_json_bytes(operation).decode()
    coordinator = MarkdownCoordinator(root, state_root)
    result = compile_pipeline_apply.apply_compile_plan(
        batch.inputs, plan, action_key="c" * 64, trigger="manual",
        coordinator=coordinator, completed_at="2026-07-14T12:00:00Z", batch=batch,
        provider_budget={"provider": "fake", "model": "fake-v1", "max_output_tokens": 4000},
    )
    part = batch.inputs.dailies[0]
    receipt = compile_pipeline_receipts.read_snapshot_receipt(part, coordinator)
    assert receipt is not None and receipt["operation_id"] == result.operation_id
    page = root / "knowledge/notes/exact-byte-pattern.md"
    assert evidence["quoted_text"] in page.read_text()
    reference = compile_pipeline_operations.evidence_binding(operation["evidence"][0], batch.inputs)["reference"]
    assert EvidenceResolver(root).resolve(reference).bytes.decode() == evidence["quoted_text"]
    return path, coordinator, part
