"""Native companion receipts remain authoritative on the adopted writer path."""

import compile_receipts
import pytest

from tests.native_compile_companion_adopted_helpers import _committed_companion, _pending_batch
from tests.native_compile_uses_whole_container_helpers import _apply_controlled_batch


def test_adopted_publication_preserves_committed_v1_companion(tmp_path, monkeypatch):
    root, coordinator, first, old, path, content = _committed_companion(tmp_path, monkeypatch)
    batch, plan = _pending_batch(root, coordinator, first)
    result = _apply_controlled_batch(batch, plan, coordinator, "b" * 64)
    assert result.state == "committed"
    assert path.read_bytes() == content
    assert "v1" in old["packing"]["algorithm"]
    receipts = [compile_receipts.receipt_predicate(coordinator).receipt(part) for part in batch.inputs.dailies]
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
