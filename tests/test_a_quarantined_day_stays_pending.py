"""The mirror names only receipted days, and a day an old quarantine hid is offered again.

The quarantine commit wrote the day's digest into the diagnostic mirror with no
receipt, and selection skipped the day for ever. See
docs/research/2026-09-25-a-quarantined-day-stays-pending.md.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import compile_pipeline
import pytest
from llm_wiki import compile_pipeline_claims, compile_pipeline_selection, compile_pipeline_sources
from markdown_transaction import MarkdownChange, MarkdownCoordinator
from memory_state import load_state, update_state

from tests.compile_transactions_helpers import _daily, vault  # noqa: F401


@pytest.fixture
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))


def _quarantine_commit(root: Path, state_root: Path) -> tuple[MarkdownCoordinator, int]:
    """A commit as the quarantine path makes it: a candidate, under its operation id."""
    coordinator = MarkdownCoordinator(root, state_root)
    path = "knowledge/inbox/claims/held.md"
    (root / "knowledge/inbox/claims").mkdir(parents=True, exist_ok=True)
    transaction = coordinator.prepare(
        [MarkdownChange.create(path, b"---\ntype: claim-candidate\n---\n")],
        operation_id=compile_pipeline_claims.QUARANTINE_OPERATION_PREFIX + "a" * 64,
        preconditions={path: "absent"},
    )
    coordinator.apply(transaction.id)
    with coordinator._connect() as database:
        row = database.execute('SELECT rowid FROM "transaction" WHERE id=?', (transaction.id,)).fetchone()
    return coordinator, int(row[0])


def test_a_batch_without_receipts_writes_nothing_to_the_mirror(vault, state_file) -> None:  # noqa: F811 - the fixture
    root, state_root = vault
    daily = _daily(root)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])

    digests = compile_pipeline.mirror_digests(SimpleNamespace(inputs=inputs), MarkdownCoordinator(root, state_root))

    assert digests == {}


def test_a_day_an_old_quarantine_hid_is_offered_again(vault, state_file) -> None:  # noqa: F811 - the fixture
    root, state_root = vault
    daily = _daily(root)
    coordinator, sequence = _quarantine_commit(root, state_root)
    hidden = {daily.name: compile_pipeline.sha256_bytes(daily.read_bytes())}
    commit = {daily.name: {"committed_at": "2026-09-02T08:13:40Z", "sequence": sequence}}
    update_state(lambda state: state.update({"compiled_daily_hashes": hidden, "compiled_daily_commits": commit}))

    compile_pipeline_selection.repair_compile_mirror(coordinator)

    state = load_state()
    assert (state["compiled_daily_hashes"], state["compiled_daily_commits"]) == ({}, {})
    assert compile_pipeline_selection.select_dailies(Namespace(file=None), state, coordinator=coordinator) == [daily]


def test_a_mirror_only_day_without_a_quarantine_behind_it_is_kept(vault, state_file) -> None:  # noqa: F811 - the fixture
    root, state_root = vault
    daily = _daily(root)
    digest = compile_pipeline.sha256_bytes(daily.read_bytes())
    update_state(lambda state: state.update({"compiled_daily_hashes": {daily.name: digest}}))

    compile_pipeline_selection.repair_compile_mirror(MarkdownCoordinator(root, state_root))

    assert load_state()["compiled_daily_hashes"] == {daily.name: digest}
