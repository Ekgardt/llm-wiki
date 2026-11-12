"""Part receipt selection and native atomic reconstruction have distinct roles."""
import bounded_io
import compile_pipeline
import compile_receipts
from llm_wiki import (
    compile_pipeline_receipts,
    compile_pipeline_selection,
    compile_pipeline_sources,
    compile_receipt_authority,
)

from tests.compile_paths import point_compile_path
from tests.compile_receipt_source_lifetime_helpers import _part_snapshots
from tests.native_compile_companion_adopted_helpers import _committed_companion, _pending_batch
from tests.native_compile_uses_whole_container_helpers import _apply_controlled_batch


def _descriptors(parts):
    return [compile_pipeline_receipts.v4_source_descriptor(part) for part in parts]


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
    selector = compile_receipts.receipt_predicate(coordinator)
    ordinary_parts = _part_snapshots(selector, first.logical_path)
    assert not _has_frames(ordinary_parts)
    assert _all_receipts(selector, _selected_parts(ordinary_parts, batch.inputs.dailies))
    captured = compile_pipeline_sources.snapshot_compile_inputs([root / first.logical_path])
    assert _has_frames(captured.dailies)
    assert _descriptors(ordinary_parts) == _descriptors(captured.dailies)
    assert _all_receipts(selector, _selected_parts(captured.dailies, batch.inputs.dailies))
    pending = compile_pipeline_sources.snapshot_compile_inputs([root / first.logical_path], compiled=selector)
    assert not _selected_parts(pending.dailies, batch.inputs.dailies)


def test_pending_native_parts_keep_already_committed_companions(tmp_path, monkeypatch):
    root, coordinator, first, _old, _path, _content = _committed_companion(tmp_path, monkeypatch)
    selector = compile_receipts.receipt_predicate(coordinator)
    assert selector.receipt(first) is not None
    pending = compile_pipeline_sources.snapshot_compile_inputs([root / first.logical_path], compiled=selector)
    assert first.part_key in {part.part_key for part in pending.dailies}
    assert any(selector.receipt(part) is None for part in pending.dailies)
    batch, _plan = _pending_batch(root, coordinator, first)
    units = compile_pipeline_sources.native_part_units(batch.inputs.dailies)
    assert units == [list(batch.inputs.dailies)]


def test_one_compile_reuses_discovery_parsing_but_rereads_receipt_bytes(tmp_path, monkeypatch):
    from contextlib import nullcontext
    root, coordinator, *_ = _committed_companion(tmp_path, monkeypatch)
    original = compile_pipeline.parse_compile_receipt_v4
    parsed = []
    reads = []
    read = bounded_io.read_stable_bytes
    def read_current(*args, **kwargs):
        reads.append(args[0])
        return read(*args, **kwargs)
    point_compile_path(monkeypatch, 'read_stable_bytes', read_current)
    monkeypatch.setattr(compile_receipt_authority, 'read_stable_bytes', read_current)
    def parse(raw, **kwargs):
        parsed.append(raw)
        return original(raw, **kwargs)
    point_compile_path(monkeypatch, 'parse_compile_receipt_v4', parse)
    scope = getattr(compile_pipeline_selection, 'receipt_discovery_scope', nullcontext)
    with scope():
        compile_receipts.receipt_predicate(coordinator)._load()
        first = len(parsed)
        assert first > 0
        compile_receipts.receipt_predicate(coordinator)._load()
        assert len(parsed) == first
        assert len(reads) == first * 2
    compile_receipts.receipt_predicate(coordinator)._load()
    assert len(parsed) == first * 2


def test_discovery_rejects_changed_receipt_even_with_preserved_mtime(tmp_path, monkeypatch):
    import os

    import pytest
    root, coordinator, *_ = _committed_companion(tmp_path, monkeypatch)
    path = next((root / 'knowledge/daily/receipts').glob('v4-*.md'))
    before = path.stat()
    with compile_pipeline_selection.receipt_discovery_scope():
        compile_receipts.receipt_predicate(coordinator)._load()
        path.write_bytes(path.read_bytes().replace(b'completed', b'corrupted'))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with pytest.raises(ValueError):
            compile_receipts.receipt_predicate(coordinator)._load()


def test_discovery_descriptor_mutation_cannot_change_later_selection(tmp_path, monkeypatch):
    root, coordinator, first, *_ = _committed_companion(tmp_path, monkeypatch)
    key = (first.logical_path, first.sha256)
    with compile_pipeline_selection.receipt_discovery_scope():
        selection = compile_receipts.receipt_predicate(coordinator)
        selection._load()
        selection.catalog[key][0][1]['sha256'] = 'f' * 64
        current = compile_receipts.receipt_predicate(coordinator)
        current._load()
        assert current.catalog[key][0][1]['sha256'] == first.sha256
        assert current.receipt(first) is not None


def test_discovery_refuses_schema_change_and_resets_after_exception(tmp_path, monkeypatch):
    import pytest
    root, coordinator, *_ = _committed_companion(tmp_path, monkeypatch)
    schema = tmp_path / 'schema.json'
    schema.write_bytes(compile_pipeline.COMPILE_RECEIPT_V4_SCHEMA.read_bytes())
    point_compile_path(monkeypatch, 'COMPILE_RECEIPT_V4_SCHEMA', schema)
    with pytest.raises(ValueError, match='schema changed'):
        with compile_pipeline_selection.receipt_discovery_scope():
            compile_receipts.receipt_predicate(coordinator)._load()
            schema.write_bytes(schema.read_bytes() + b'\n')
            compile_receipts.receipt_predicate(coordinator)._load()
    assert compile_receipts.RECEIPT_DISCOVERY_PARSING.get() is None


def test_discovery_reuse_does_not_reuse_transaction_authority(tmp_path, monkeypatch):
    import pytest
    root, coordinator, first, *_ = _committed_companion(tmp_path, monkeypatch)
    def refused(*args, **kwargs):
        raise ValueError('authority changed')
    with compile_pipeline_selection.receipt_discovery_scope():
        assert compile_receipts.receipt_predicate(coordinator).receipt(first) is not None
        monkeypatch.setattr(compile_receipt_authority, 'require_transaction_authority', refused)
        with pytest.raises(ValueError, match='authority changed'):
            compile_receipts.receipt_predicate(coordinator).receipt(first)
