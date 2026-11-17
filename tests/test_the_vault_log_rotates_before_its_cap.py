"""A compile archives and restarts the vault log instead of failing at its cap.

docs/research/2026-09-25-the-vault-log-rotates-before-its-cap.md
"""
from __future__ import annotations

from llm_wiki import compile_pipeline_apply, compile_pipeline_sources
from markdown_transaction import MarkdownCoordinator
from vault_log import vault_log_path

from tests.compile_transactions_helpers import (  # noqa: F401
    _daily,
    _semantic_plan,
    vault,  # noqa: F401
)

ENTRY = b"- 2026-07-01 \xe2\x80\x94 Manual compile completed for snapshot a. Touched: none.\n"


def _compile(root, state_root):
    inputs = compile_pipeline_sources.snapshot_compile_inputs([_daily(root)])
    return inputs, compile_pipeline_apply.apply_compile_plan(
        inputs,
        _semantic_plan(),
        action_key="a" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:00:00Z",
    )


def test_a_log_past_the_rotation_size_is_archived_whole_in_the_same_compile(vault):  # noqa: F811
    root, state_root = vault
    old_log = b"# Session Memory Log\n\n" + ENTRY * (compile_pipeline_apply.LOG_ROTATE_BYTES // len(ENTRY) + 1)
    vault_log_path(root).write_bytes(old_log)

    inputs, result = _compile(root, state_root)

    archive = root / "knowledge/log-archive/log.local.2026-07-14.md"
    fresh = vault_log_path(root).read_bytes()
    assert (result.state, archive.read_bytes() == old_log) == ("committed", True)
    assert (len(fresh) < 1024, b"log-archive/log.local.2026-07-14.md" in fresh) == (True, True)
    assert inputs.dailies[0].sha256.encode() in fresh


def test_a_small_log_is_appended_as_before(vault):  # noqa: F811
    root, state_root = vault

    _compile(root, state_root)

    assert not (root / "knowledge/log-archive").exists()
