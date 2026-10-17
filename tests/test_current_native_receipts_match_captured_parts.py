"""Part receipt selection and native atomic reconstruction have distinct roles."""
import compile_memory as compiler

from tests.test_native_compile_companion_adopted import (
    _committed_companion,
    _pending_batch,
)
from tests.test_native_compile_uses_whole_container import _apply_controlled_batch


def _descriptors(parts):
    return [compiler._v4_source_descriptor(part) for part in parts]


def _all_receipts(selector, parts):
    return all(selector.receipt(part) for part in parts)


def _has_frames(parts):
    return any(part.native_frames for part in parts)


def _selected_parts(parts, selected):
    keys = {part.part_key for part in selected}
    return [part for part in parts if part.part_key in keys]


def test_committed_native_parts_match_without_selector_frames(tmp_path, monkeypatch):
    root, coordinator, first, _old, _path, _content = _committed_companion(tmp_path, monkeypatch)
    batch, plan = _pending_batch(root, coordinator, first)
    result = _apply_controlled_batch(batch, plan, coordinator, "b" * 64)
    assert result.state == "committed"
    selector = compiler._receipt_predicate(coordinator)
    ordinary_parts = selector._source_parts(first.logical_path)
    assert not _has_frames(ordinary_parts)
    assert _all_receipts(selector, _selected_parts(ordinary_parts, batch.inputs.dailies))
    captured = compiler.snapshot_compile_inputs([root / first.logical_path])
    assert _has_frames(captured.dailies)
    assert _descriptors(ordinary_parts) == _descriptors(captured.dailies)
    assert _all_receipts(selector, _selected_parts(captured.dailies, batch.inputs.dailies))
    pending = compiler.snapshot_compile_inputs([root / first.logical_path], compiled=selector)
    assert not _selected_parts(pending.dailies, batch.inputs.dailies)


def test_pending_native_parts_keep_already_committed_companions(tmp_path, monkeypatch):
    root, coordinator, first, _old, _path, _content = _committed_companion(tmp_path, monkeypatch)
    selector = compiler._receipt_predicate(coordinator)
    assert selector.receipt(first) is not None
    pending = compiler.snapshot_compile_inputs([root / first.logical_path], compiled=selector)
    assert first.part_key in {part.part_key for part in pending.dailies}
    assert any(selector.receipt(part) is None for part in pending.dailies)
    batch, _plan = _pending_batch(root, coordinator, first)
    units = compiler._native_part_units(batch.inputs.dailies)
    assert units == [list(batch.inputs.dailies)]
