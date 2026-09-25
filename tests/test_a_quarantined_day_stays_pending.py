"""A quarantined batch leaves its day pending, and a day it once hid is offered again.

The quarantine commit wrote the day's digest into the diagnostic mirror with no
receipt, and selection skipped the day for ever. See
docs/research/2026-09-25-a-quarantined-day-stays-pending.md.
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import compile_memory
import memory_state
import pytest
from claims import ClaimIndex
from markdown_transaction import MarkdownCoordinator
from memory_state import load_state, update_state
from reliable_memory import canonical_json_bytes

from tests.test_compile_transactions import (
    _claim_record,
    _daily,
    _semantic_plan,
    vault,  # noqa: F401 - the fixture is used by name
)


@pytest.fixture
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run/state.json")


def _quarantined_compile(root: Path, state_root: Path):
    """A batch whose inferred claim conflicts with a user claim: it is quarantined."""
    daily = _daily(root)
    old = _claim_record(root, claim_id="old", value="blue", text="The prior state is blue.", authority="user")
    (root / "knowledge/notes/existing.md").write_bytes(
        b"---\ntype: concept\n---\n# Existing\n\n## Claims\n```json\n"
        + canonical_json_bytes({"schema_version": "claim-ledger/v1", "claims": [old]})
        + b"\n```\n"
    )
    new = _claim_record(
        root, claim_id="new", value="red", text="A durable exact-byte observation.", authority="inferred"
    )
    operation = json.loads(str(_semantic_plan()["operations"][0]["content"]))
    operation["claims"] = [new]
    plan = {
        "schema_version": "compile-plan/v2",
        "operations": [
            {"kind": "create", "path": "knowledge/notes/exact-byte-pattern.md",
             "content": canonical_json_bytes(operation).decode()}
        ],
    }
    ClaimIndex(state_root, vault=root).rebuild()
    coordinator = MarkdownCoordinator(root, state_root)
    inputs = compile_memory.snapshot_compile_inputs([daily])
    result = compile_memory.apply_compile_plan(
        inputs, plan, action_key="9" * 64, trigger="manual",
        coordinator=coordinator, completed_at="2026-07-14T12:00:00Z",
    )
    assert result.operation_id.startswith(compile_memory.QUARANTINE_OPERATION_PREFIX)
    return daily, inputs, result, coordinator


def test_a_quarantined_batch_writes_nothing_to_the_mirror(vault, state_file) -> None:  # noqa: F811 - the fixture
    daily, inputs, _result, coordinator = _quarantined_compile(*vault)

    digests = compile_memory._mirror_digests(SimpleNamespace(inputs=inputs), coordinator)

    assert digests == {}
    assert compile_memory.select_dailies(Namespace(file=None), load_state(), coordinator=coordinator) == [daily]


def test_a_day_an_old_quarantine_hid_is_offered_again(vault, state_file) -> None:  # noqa: F811 - the fixture
    daily, inputs, result, coordinator = _quarantined_compile(*vault)
    hidden = {daily.name: inputs.dailies[0].sha256}
    commit = {daily.name: {"committed_at": result.committed_at, "sequence": result.commit_sequence}}
    update_state(lambda state: state.update({"compiled_daily_hashes": hidden, "compiled_daily_commits": commit}))

    compile_memory._repair_compile_mirror(coordinator)

    state = load_state()
    assert (state["compiled_daily_hashes"], state["compiled_daily_commits"]) == ({}, {})
    assert compile_memory.select_dailies(Namespace(file=None), state, coordinator=coordinator) == [daily]


def test_a_mirror_only_day_without_a_quarantine_behind_it_is_kept(vault, state_file) -> None:  # noqa: F811 - the fixture
    root, state_root = vault
    daily = _daily(root)
    digest = compile_memory.sha256_bytes(daily.read_bytes())
    update_state(lambda state: state.update({"compiled_daily_hashes": {daily.name: digest}}))

    compile_memory._repair_compile_mirror(MarkdownCoordinator(root, state_root))

    assert load_state()["compiled_daily_hashes"] == {daily.name: digest}
