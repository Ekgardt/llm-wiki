"""Native companion receipts remain authoritative on the adopted writer path."""
from dataclasses import replace

import compile_memory as compiler
import pytest
from markdown_transaction import active_markdown_coordinator

from tests.adopted_vault import adopt
from tests.test_native_compile_uses_whole_container import (
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
    paths = {"ROOT": root, "STATE_ROOT": state, "MEMORY": root / "knowledge", "DAILY_DIR": root / "knowledge/daily", "KNOWLEDGE": root / "knowledge/notes", "INDEX": root / "knowledge/index.md", "LOG": root / "knowledge/log.md", "AGENTS": root / "AGENTS.md"}
    for name, path in paths.items():
        monkeypatch.setattr(compiler, name, path)
    compiler.INDEX.write_bytes(b"# Index\n")
    compiler.LOG.write_bytes(b"# Log\n")
    compiler.AGENTS.write_bytes(b"contract\n")
    return root, active_markdown_coordinator(root, state)


def _committed_companion(tmp_path, monkeypatch):
    root, coordinator = _adopted_environment(tmp_path, monkeypatch)
    inputs, _ = _native_inputs(root, monkeypatch, "A" * 18000 + "\nМой велосипед синий.")
    first = _part_with_key(inputs.dailies, _frame_covering_keys(inputs, inputs.dailies[0].original_content))
    historical = replace(inputs, dailies=(replace(first, native_frames=()),), sources=())
    old_batch = compiler.pack_compile_batches(historical, model=None)[0]
    result = _apply_controlled_batch(old_batch, compiler._normalize_plan([], old_batch.inputs), coordinator, "a" * 64)
    assert result.state == "committed"
    receipt = compiler._receipt_predicate(coordinator).receipt(first)
    path = root / f"knowledge/daily/receipts/v4-{receipt['source_identity']}.md"
    return root, coordinator, first, receipt, path, path.read_bytes()


def _pending_batch(root, coordinator, first):
    current = compiler.snapshot_compile_inputs([root / first.logical_path], compiled=compiler._receipt_predicate(coordinator))
    keys = _frame_covering_keys(current, first.original_content)
    batch = compiler.pack_compile_batches(compiler._subset_compile_inputs(current, keys), model=None)[0]
    operation = _native_operation(batch.inputs, "Мой велосипед синий.")
    plan = compiler._normalize_plan(compiler._with_derived_claims([operation], batch.inputs), batch.inputs)
    return batch, plan


def test_adopted_publication_preserves_committed_v1_companion(tmp_path, monkeypatch):
    root, coordinator, first, old, path, content = _committed_companion(tmp_path, monkeypatch)
    batch, plan = _pending_batch(root, coordinator, first)
    result = _apply_controlled_batch(batch, plan, coordinator, "b" * 64)
    assert result.state == "committed"
    assert path.read_bytes() == content
    assert "v1" in old["packing"]["algorithm"]
    receipts = [compiler._receipt_predicate(coordinator).receipt(part) for part in batch.inputs.dailies]
    assert all(receipts)
    assert {record["operation_id"] for record in receipts} == {old["operation_id"], result.operation_id}
    assert {record["packing"]["algorithm"] for record in receipts} == {"compile-complete-items/v1", "compile-complete-items/v2"}
    assert coordinator.committed_attempt(old["operation_id"]).state == "committed"


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_adopted_publication_refuses_damaged_companion(tmp_path, monkeypatch, damage):
    root, coordinator, first, old, path, content = _committed_companion(tmp_path, monkeypatch)
    batch, plan = _pending_batch(root, coordinator, first)
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(content + b"external corruption\n")
    with pytest.raises(ValueError):
        _apply_controlled_batch(batch, plan, coordinator, "b" * 64)
    assert coordinator.committed_attempt(old["operation_id"]).state == "committed"
    assert not list((root / "knowledge/notes").glob("*.md"))
