"""Part receipt selection and native atomic reconstruction have distinct roles."""
import compile_memory as compiler

from tests.test_compile_receipt_source_lifetime import _part_snapshots
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
    ordinary_parts = _part_snapshots(selector, first.logical_path)
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


def test_one_compile_reuses_discovery_parsing_but_rereads_receipt_bytes(tmp_path, monkeypatch):
    from contextlib import nullcontext
    root, coordinator, *_ = _committed_companion(tmp_path, monkeypatch)
    original = compiler.parse_compile_receipt_v4
    parsed = []
    reads = []
    read = compiler.read_stable_bytes
    def read_current(*args, **kwargs):
        reads.append(args[0])
        return read(*args, **kwargs)
    monkeypatch.setattr(compiler, 'read_stable_bytes', read_current)
    def parse(raw, **kwargs):
        parsed.append(raw)
        return original(raw, **kwargs)
    monkeypatch.setattr(compiler, 'parse_compile_receipt_v4', parse)
    scope = getattr(compiler, '_receipt_discovery_scope', nullcontext)
    with scope():
        compiler._receipt_predicate(coordinator)._load()
        first = len(parsed)
        assert first > 0
        compiler._receipt_predicate(coordinator)._load()
        assert len(parsed) == first
        assert len(reads) == first * 2
    compiler._receipt_predicate(coordinator)._load()
    assert len(parsed) == first * 2


def test_discovery_rejects_changed_receipt_even_with_preserved_mtime(tmp_path, monkeypatch):
    import os

    import pytest
    root, coordinator, *_ = _committed_companion(tmp_path, monkeypatch)
    path = next((root / 'knowledge/daily/receipts').glob('v4-*.md'))
    before = path.stat()
    with compiler._receipt_discovery_scope():
        compiler._receipt_predicate(coordinator)._load()
        path.write_bytes(path.read_bytes().replace(b'completed', b'corrupted'))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with pytest.raises(ValueError):
            compiler._receipt_predicate(coordinator)._load()


def test_discovery_descriptor_mutation_cannot_change_later_selection(tmp_path, monkeypatch):
    root, coordinator, first, *_ = _committed_companion(tmp_path, monkeypatch)
    key = (first.logical_path, first.sha256)
    with compiler._receipt_discovery_scope():
        selection = compiler._receipt_predicate(coordinator)
        selection._load()
        selection.catalog[key][0][1]['sha256'] = 'f' * 64
        current = compiler._receipt_predicate(coordinator)
        current._load()
        assert current.catalog[key][0][1]['sha256'] == first.sha256
        assert current.receipt(first) is not None


def test_discovery_refuses_schema_change_and_resets_after_exception(tmp_path, monkeypatch):
    import pytest
    root, coordinator, *_ = _committed_companion(tmp_path, monkeypatch)
    schema = tmp_path / 'schema.json'
    schema.write_bytes(compiler.COMPILE_RECEIPT_V4_SCHEMA.read_bytes())
    monkeypatch.setattr(compiler, 'COMPILE_RECEIPT_V4_SCHEMA', schema)
    with pytest.raises(ValueError, match='schema changed'):
        with compiler._receipt_discovery_scope():
            compiler._receipt_predicate(coordinator)._load()
            schema.write_bytes(schema.read_bytes() + b'\n')
            compiler._receipt_predicate(coordinator)._load()
    assert compiler._RECEIPT_DISCOVERY_PARSING.get() is None


def test_discovery_reuse_does_not_reuse_transaction_authority(tmp_path, monkeypatch):
    import pytest
    root, coordinator, first, *_ = _committed_companion(tmp_path, monkeypatch)
    def refused(*args, **kwargs):
        raise ValueError('authority changed')
    with compiler._receipt_discovery_scope():
        assert compiler._receipt_predicate(coordinator).receipt(first) is not None
        monkeypatch.setattr(compiler, '_require_transaction_authority', refused)
        with pytest.raises(ValueError, match='authority changed'):
            compiler._receipt_predicate(coordinator).receipt(first)
