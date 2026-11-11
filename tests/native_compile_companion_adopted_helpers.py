"""Helpers shared by test_native_compile_companion_adopted.py and the test files importing them."""

from dataclasses import replace

import compile_receipts
import vault_log
from llm_wiki import (
    compile_pipeline_batches,
    compile_pipeline_operations,
    compile_pipeline_prompts,
    compile_pipeline_sources,
    runtime_roots,
)
from markdown_transaction import active_markdown_coordinator

from tests.adopted_vault import adopt
from tests.native_compile_uses_whole_container_helpers import (
    _apply_controlled_batch,
    _frame_covering_keys,
    _native_inputs,
    _native_operation,
    _part_with_key,
)


def _adopted_environment(tmp_path, monkeypatch):
    root, state = adopt(tmp_path)
    for relative in ("knowledge/daily/receipts", "knowledge/notes", "knowledge/projects"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    runtime_roots.index_file().write_bytes(b"# Index\n")
    vault_log.vault_log_path(runtime_roots.vault_root()).write_bytes(b"# Log\n")
    (root / "AGENTS.md").write_bytes(b"contract\n")
    return root, active_markdown_coordinator(root, state)


def _committed_companion(tmp_path, monkeypatch):
    root, coordinator = _adopted_environment(tmp_path, monkeypatch)
    inputs, _ = _native_inputs(root, monkeypatch, "A" * 18000 + "\nМой велосипед синий.")
    first = _part_with_key(inputs.dailies, _frame_covering_keys(inputs, inputs.dailies[0].original_content))
    historical = replace(inputs, dailies=(replace(first, native_frames=()),), sources=())
    old_batch = compile_pipeline_batches.pack_compile_batches(historical, model=None)[0]
    result = _apply_controlled_batch(old_batch, compile_pipeline_prompts.normalize_plan([], old_batch.inputs), coordinator, "a" * 64)
    assert result.state == "committed"
    receipt = compile_receipts.receipt_predicate(coordinator).receipt(first)
    path = root / f"knowledge/daily/receipts/v4-{receipt['source_identity']}.md"
    return root, coordinator, first, receipt, path, path.read_bytes()


def _pending_batch(root, coordinator, first):
    current = compile_pipeline_sources.snapshot_compile_inputs([root / first.logical_path], compiled=compile_receipts.receipt_predicate(coordinator))
    keys = _frame_covering_keys(current, first.original_content)
    batch = compile_pipeline_batches.pack_compile_batches(compile_pipeline_sources.subset_compile_inputs(current, keys), model=None)[0]
    operation = _native_operation(batch.inputs, "Мой велосипед синий.")
    plan = compile_pipeline_prompts.normalize_plan(compile_pipeline_operations.with_derived_claims([operation], batch.inputs), batch.inputs)
    return batch, plan
