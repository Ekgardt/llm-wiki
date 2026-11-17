"""A context-bound receipt must survive cleanup and reach the compile trigger."""
from __future__ import annotations

import maybe_compile
from llm_wiki import compile_pipeline_receipts, compile_pipeline_selection
from reliable_memory import sha256_bytes

from tests.a_continuation_keeps_its_original_entry_helpers import _published_continuation
from tests.compile_transactions_helpers import vault as vault


def test_operator_discard_preserves_a_valid_committed_context_receipt(vault):
    _daily, coordinator, part = _published_continuation(vault)
    receipt = compile_pipeline_receipts.context_receipt_path(compile_pipeline_receipts.v4_source_descriptor(part))
    before = receipt.read_bytes()
    discarded = compile_pipeline_selection.discard_unusable_receipts()
    assert discarded == []
    assert receipt.read_bytes() == before
    assert compile_pipeline_receipts.read_snapshot_receipt(part, coordinator) is not None


def test_matching_mirror_without_authority_still_triggers_compile(tmp_path, monkeypatch):
    daily = tmp_path / "knowledge/daily/2026-07-14.md"
    daily.parent.mkdir(parents=True)
    content = b"## [10:00:00] session-end | manual\nA durable observation.\n"
    daily.write_bytes(content)
    state = {"compiled_daily_hashes": {daily.name: sha256_bytes(content)}}
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(maybe_compile, "load_state", lambda: state)
    assert maybe_compile.has_pending_work()
