"""Helpers shared by test_native_compile_uses_whole_container.py and the test files importing them."""

from llm_wiki import compile_pipeline_apply, compile_pipeline_sources

from tests.compile_claims_producer_helpers import _candidate, _operation
from tests.compile_paths import point_compile_path
from tests.modules.native_event_reader.contract_helpers import _frame
from tests.native_user_frames_keep_their_physical_evidence_helpers import _captured_vault


def _native_inputs(root, monkeypatch, prompt):
    snapshot = _captured_vault(root, _frame(prompt))
    path = "knowledge/daily/2026-09-29.md"
    target = root / path
    target.write_bytes(target.read_bytes().replace(b"# Daily\n", b"# 2026-09-29\n"))
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    point_compile_path(monkeypatch, "_vault_file_snapshots", lambda add: ())
    point_compile_path(monkeypatch, "_knowledge_targets", lambda add: ())
    return compile_pipeline_sources.snapshot_compile_inputs([target]), snapshot


def _native_operation(inputs, quote):
    part = inputs.dailies[0]
    start = part.original_content.index(b'    {"') + 4
    operation = _operation([_candidate(subject="bicycle", relation="has-state",
                                      value={"type": "string", "value": "blue"})])
    operation["evidence"] = [{
        "daily_date": "2026-09-29", "timestamp": "00:00:00",
        "quoted_text": quote, "claim": "User owns a test bicycle.",
        "native_event": {"source_path": part.logical_path,
                         "byte_start": start, "line_index": 1},
    }]
    return operation


def _frame_covering_keys(inputs, raw):
    start = raw.index(b'    {"') + 4
    end = raw.index(b"\n", start)
    return {part.part_key for part in inputs.dailies if part.byte_start < end and part.byte_end > start}


def _part_with_key(parts, keys):
    return next(part for part in parts if part.part_key in keys)


def _apply_controlled_batch(batch, plan, coordinator, action_key):
    return compile_pipeline_apply.apply_compile_plan(
        batch.inputs, plan, action_key=action_key, trigger="manual",
        coordinator=coordinator, batch=batch,
        provider_budget={"provider": "fake", "model": "fake-v1", "max_output_tokens": 4000},
    )
