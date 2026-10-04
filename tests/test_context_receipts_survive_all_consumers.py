"""A context-bound receipt must survive cleanup and reach the compile trigger."""
from __future__ import annotations

import compile_memory as compiler
import maybe_compile
from reliable_memory import sha256_bytes

from tests.test_a_continuation_keeps_its_original_entry import _published_continuation
from tests.test_compile_transactions import vault as vault


def test_operator_discard_preserves_a_valid_committed_context_receipt(vault):
    _daily, coordinator, part = _published_continuation(vault)
    receipt = compiler._context_receipt_path(compiler._v4_source_descriptor(part))
    before = receipt.read_bytes()
    discarded = compiler.discard_unusable_receipts()
    assert discarded == []
    assert receipt.read_bytes() == before
    assert compiler._read_snapshot_receipt(part, coordinator) is not None


def test_matching_mirror_without_authority_still_triggers_compile(tmp_path, monkeypatch):
    daily = tmp_path / "knowledge/daily/2026-07-14.md"
    daily.parent.mkdir(parents=True)
    content = b"## [10:00:00] session-end | manual\nA durable observation.\n"
    daily.write_bytes(content)
    state = {"compiled_daily_hashes": {daily.name: sha256_bytes(content)}}
    monkeypatch.setattr(maybe_compile, "ROOT", tmp_path)
    monkeypatch.setattr(maybe_compile, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(maybe_compile, "load_state", lambda: state)
    assert maybe_compile._has_pending_work()
