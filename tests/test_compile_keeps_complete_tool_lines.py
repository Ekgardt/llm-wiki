"""Whole captured tool lines retain ordinary physical evidence authority."""
from dataclasses import replace

import compile_pipeline
import compile_receipts
import pytest
from evidence_resolver import EvidenceRef
from llm_wiki import (
    compile_pipeline_operations,
    compile_pipeline_receipts,
    compile_pipeline_sources,
)

from tests.compile_keeps_complete_tool_lines_helpers import _tool_parts


def test_split_tool_json_is_one_atomic_source_unit():
    parts, raw = _tool_parts()
    assert len(parts) == 2
    assert len(raw.decode()) < 16384
    assert len(raw) > compile_pipeline.MAX_DAILY_PART_BYTES
    units = compile_pipeline_sources.native_part_units(parts)
    assert len(units) == 1
    assert units[0] == parts
    assert all(not part.native_frames for part in parts)


def test_pending_tool_half_keeps_its_committed_companion():
    parts, _ = _tool_parts()
    pending = compile_pipeline_sources.pending_with_native_context(parts, lambda path, digest: digest == parts[0].sha256)
    assert len(pending) == 2
    assert pending[0].already_compiled
    assert not pending[1].already_compiled


@pytest.mark.parametrize("index", [0, 1])
def test_missing_tool_half_is_refused_before_model_layout(index):
    parts, _ = _tool_parts()
    with pytest.raises(ValueError, match="tool line requires all covering"):
        compile_pipeline_sources.deduplicated_sources([parts[index]])


def test_complete_tool_source_preserves_raw_json_and_every_part_manifest():
    parts, raw = _tool_parts()
    source = compile_pipeline_sources.deduplicated_sources(parts)[0]
    assert source.content == b"".join(part.content for part in parts)
    assert source.prompt_content is None
    assert raw in source.content
    assert source.sha256 == compile_pipeline.sha256_bytes(source.content)
    manifest = compile_pipeline_receipts.v4_manifest(compile_pipeline_sources.CompileInputs(tuple(parts), (), ()))
    assert len(manifest) == len(parts)
    assert [item['byte_start'] for item in manifest] == [part.byte_start for part in parts]


def test_forged_tool_companion_is_not_authorized_by_line_grouping():
    parts, _ = _tool_parts()
    forged = [parts[0], replace(parts[1], content=b"forged")]
    with pytest.raises(ValueError, match="bytes differ"):
        compile_pipeline_sources.deduplicated_sources(forged)


def test_plain_unknown_json_keeps_existing_partition_contract():
    raw = b"# 2026-10-02\n\n## 12:34:56\n    {\"unknown\":\"" + ("я" * 12000).encode() + b'\"}\n'
    parts = compile_pipeline_sources.daily_parts("knowledge/daily/2026-10-02.md", raw)
    assert len(compile_pipeline_sources.native_part_units(parts)) == len(parts)


@pytest.mark.parametrize("value", [b"NaN", b"Infinity", b"-Infinity"])
def test_noncanonical_tool_json_does_not_gain_grouping_authority(value):
    line = b'    {"event_type":"post_tool_use","payload":' + value + b'}'
    assert not compile_receipts.is_physical_tool_line(line)


def test_joined_tool_quote_binds_exact_complete_physical_line(tmp_path, monkeypatch):
    parts, raw = _tool_parts()
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    inputs = compile_pipeline_sources.CompileInputs(tuple(parts), (), ())
    item = {"daily_date": "2026-10-02", "timestamp": "12:34:56",
            "quoted_text": raw.decode(), "claim": "Recorded tool target."}
    binding = compile_pipeline_operations.evidence_binding(item, inputs)
    reference = EvidenceRef.parse(binding['reference'])
    assert reference.source_sha256 == parts[0].original_sha256
    assert parts[0].original_content[reference.byte_start:reference.byte_end] == raw
    assert binding['source_digest'] == parts[0].sha256


def test_tool_quote_cannot_gain_native_user_authority(tmp_path, monkeypatch):
    parts, raw = _tool_parts()
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    inputs = compile_pipeline_sources.CompileInputs(tuple(parts), (), ())
    item = {"daily_date": "2026-10-02", "timestamp": "12:34:56",
            "quoted_text": raw.decode(), "claim": "Recorded tool target.",
            "native_event": {"source_path": parts[0].logical_path,
                             "byte_start": parts[0].original_content.index(raw),
                             "line_index": 0}}
    with pytest.raises(ValueError, match="native evidence requires"):
        compile_pipeline_operations.evidence_binding(item, inputs)
