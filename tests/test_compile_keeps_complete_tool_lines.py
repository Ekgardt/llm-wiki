"""Whole captured tool lines retain ordinary physical evidence authority."""
from dataclasses import replace

import compile_memory as compiler
import pytest
from breadcrumb_decision import _journal_block
from evidence_resolver import EvidenceRef
from reliable_memory import canonical_json_bytes


def _tool_parts():
    record = {"event_type": "post_tool_use", "payload": {
        "tool_name": "exec_command", "target": "я" * 12000}}
    raw = canonical_json_bytes(record)
    anchor = {"occurred_at": "2026-10-02T12:34:56Z", "intent_id": "physical-tool-control"}
    head = "knowledge/raw/sessions/2026-10-02/physical-tool-control.breadcrumb.md"
    day = b"# 2026-10-02\n" + _journal_block(anchor, head, raw).encode()
    parts = compiler._daily_parts("knowledge/daily/2026-10-02.md", day)
    start = day.index(raw)
    covering = [part for part in parts if part.byte_start < start + len(raw)
                and part.byte_end > start]
    return covering, raw


def test_split_tool_json_is_one_atomic_source_unit():
    parts, raw = _tool_parts()
    assert len(parts) == 2
    assert len(raw.decode()) < 16384
    assert len(raw) > compiler.MAX_DAILY_PART_BYTES
    units = compiler._native_part_units(parts)
    assert len(units) == 1
    assert units[0] == parts
    assert all(not part.native_frames for part in parts)


def test_pending_tool_half_keeps_its_committed_companion():
    parts, _ = _tool_parts()
    pending = compiler._pending_with_native_context(parts, lambda path, digest: digest == parts[0].sha256)
    assert len(pending) == 2
    assert pending[0].already_compiled
    assert not pending[1].already_compiled


@pytest.mark.parametrize("index", [0, 1])
def test_missing_tool_half_is_refused_before_model_layout(index):
    parts, _ = _tool_parts()
    with pytest.raises(ValueError, match="tool line requires all covering"):
        compiler._deduplicated_sources([parts[index]])


def test_complete_tool_source_preserves_raw_json_and_every_part_manifest():
    parts, raw = _tool_parts()
    source = compiler._deduplicated_sources(parts)[0]
    assert source.content == b"".join(part.content for part in parts)
    assert source.prompt_content is None
    assert raw in source.content
    assert source.sha256 == compiler.sha256_bytes(source.content)
    manifest = compiler._v4_manifest(compiler.CompileInputs(tuple(parts), (), ()))
    assert len(manifest) == len(parts)
    assert [item['byte_start'] for item in manifest] == [part.byte_start for part in parts]


def test_forged_tool_companion_is_not_authorized_by_line_grouping():
    parts, _ = _tool_parts()
    forged = [parts[0], replace(parts[1], content=b"forged")]
    with pytest.raises(ValueError, match="bytes differ"):
        compiler._deduplicated_sources(forged)


def test_plain_unknown_json_keeps_existing_partition_contract():
    raw = b"# 2026-10-02\n\n## 12:34:56\n    {\"unknown\":\"" + ("я" * 12000).encode() + b'\"}\n'
    parts = compiler._daily_parts("knowledge/daily/2026-10-02.md", raw)
    assert len(compiler._native_part_units(parts)) == len(parts)


@pytest.mark.parametrize("value", [b"NaN", b"Infinity", b"-Infinity"])
def test_noncanonical_tool_json_does_not_gain_grouping_authority(value):
    line = b'    {"event_type":"post_tool_use","payload":' + value + b'}'
    assert not compiler._is_physical_tool_line(line)


def test_joined_tool_quote_binds_exact_complete_physical_line(tmp_path, monkeypatch):
    parts, raw = _tool_parts()
    monkeypatch.setattr(compiler, "ROOT", tmp_path)
    inputs = compiler.CompileInputs(tuple(parts), (), ())
    item = {"daily_date": "2026-10-02", "timestamp": "12:34:56",
            "quoted_text": raw.decode(), "claim": "Recorded tool target."}
    binding = compiler._evidence_binding(item, inputs)
    reference = EvidenceRef.parse(binding['reference'])
    assert reference.source_sha256 == parts[0].original_sha256
    assert parts[0].original_content[reference.byte_start:reference.byte_end] == raw
    assert binding['source_digest'] == parts[0].sha256


def test_tool_quote_cannot_gain_native_user_authority(tmp_path, monkeypatch):
    parts, raw = _tool_parts()
    monkeypatch.setattr(compiler, "ROOT", tmp_path)
    inputs = compiler.CompileInputs(tuple(parts), (), ())
    item = {"daily_date": "2026-10-02", "timestamp": "12:34:56",
            "quoted_text": raw.decode(), "claim": "Recorded tool target.",
            "native_event": {"source_path": parts[0].logical_path,
                             "byte_start": parts[0].original_content.index(raw),
                             "line_index": 0}}
    with pytest.raises(ValueError, match="native evidence requires"):
        compiler._evidence_binding(item, inputs)
