"""Sizing reuses immutable partitions; accepted evidence still binds afresh."""
from dataclasses import replace
from unittest.mock import Mock

import compile_pipeline
import evidence_resolver
import pytest
from evidence_resolver import EvidenceRef
from llm_wiki import (
    compile_pipeline_claims,
    compile_pipeline_operations,
    compile_pipeline_prompts,
    compile_pipeline_sources,
)

from tests.compile_keeps_complete_tool_lines_helpers import _tool_parts
from tests.compile_paths import point_compile_path


@pytest.fixture
def packet(tmp_path, monkeypatch):
    parts, raw = _tool_parts()
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    inputs = compile_pipeline_sources.CompileInputs(tuple(parts), (), ())
    measure = compile_pipeline_prompts.ByteBatchMeasure(inputs)
    measure.partitions.for_part(parts[0])
    item = {"daily_date": "2026-10-02", "timestamp": "12:34:56",
            "quoted_text": raw.decode(), "claim": "Recorded tool target."}
    return inputs, measure, item


def test_sizing_does_not_reparse_a_joined_tool_day_for_each_binding(packet, monkeypatch):
    inputs, measure, item = packet
    expected = compile_pipeline_operations.evidence_binding(item, inputs)
    parsing = Mock(wraps=compile_pipeline_sources.partition_for_join)
    point_compile_path(monkeypatch, "partition_for_join", parsing)
    with compile_pipeline_prompts.measure_choice_resolution(measure):
        first = compile_pipeline_operations.evidence_binding(item, inputs)
        second = compile_pipeline_operations.evidence_binding(item, inputs)
    assert parsing.call_count == 0
    assert first == second == expected
    reference = EvidenceRef.parse(first["reference"])
    physical = inputs.dailies[0].original_content[reference.byte_start:reference.byte_end]
    assert physical == item["quoted_text"].encode()
    assert first["quote_sha256"] == compile_pipeline.sha256_bytes(physical)


def test_final_binding_reparses_after_sizing_scope_ends(packet, monkeypatch):
    inputs, measure, item = packet
    with compile_pipeline_prompts.measure_choice_resolution(measure):
        expected = compile_pipeline_operations.evidence_binding(item, inputs)
    parsing = Mock(wraps=compile_pipeline_sources.partition_for_join)
    point_compile_path(monkeypatch, "partition_for_join", parsing)
    assert compile_pipeline_operations.evidence_binding(item, inputs) == expected
    assert parsing.call_count > 0
    assert compile_pipeline_sources.SOURCE_CHOICE_PARSING.get() is None


@pytest.mark.parametrize("damage", ("content", "sha256", "part_index", "original_sha256"))
def test_reused_partitions_do_not_authorize_a_forged_member(packet, damage):
    inputs, measure, item = packet
    changes = {"content": b"forged", "sha256": "0" * 64,
               "part_index": inputs.dailies[-1].part_count,
               "original_sha256": "0" * 64}
    forged = replace(inputs.dailies[-1], **{damage: changes[damage]})
    changed = replace(inputs, dailies=(*inputs.dailies[:-1], forged))
    with compile_pipeline_prompts.measure_choice_resolution(measure), pytest.raises(ValueError):
        compile_pipeline_operations.evidence_binding(item, changed)


def test_replaced_original_bytes_cannot_reuse_the_old_partition(packet):
    inputs, measure, item = packet
    changed = replace(inputs, dailies=tuple(
        replace(part, original_content=part.original_content + b"\n")
        for part in inputs.dailies))
    with compile_pipeline_prompts.measure_choice_resolution(measure), pytest.raises(ValueError):
        compile_pipeline_operations.evidence_binding(item, changed)


def test_an_arbitrary_context_object_cannot_supply_partition_authority(packet):
    inputs, _measure, _item = packet
    part = inputs.dailies[0]
    expected = compile_pipeline_sources.partition_for_join(part)
    forged = Mock()
    token = compile_pipeline_sources.SOURCE_CHOICE_PARSING.set((None, forged))
    try:
        actual = compile_pipeline_sources.join_partition(part, None)
    finally:
        compile_pipeline_sources.SOURCE_CHOICE_PARSING.reset(token)
    assert actual == expected
    forged.for_part.assert_not_called()


def test_joined_binding_reuses_canonical_entry_boundaries(packet, monkeypatch):
    inputs, measure, item = packet
    expected = compile_pipeline_operations.evidence_binding(item, inputs)
    with compile_pipeline_prompts.measure_choice_resolution(measure):
        measure.choice_resolver._immutable_byte_proof(inputs.dailies[0].original_content)
        parsing = Mock(wraps=evidence_resolver.daily_entries)
        point_compile_path(monkeypatch, "daily_entries", parsing)
        assert compile_pipeline_operations.evidence_binding(item, inputs) == expected
        assert compile_pipeline_operations.evidence_binding(item, inputs) == expected
    assert parsing.call_count == 0


@pytest.mark.parametrize("timestamp", ("12:34:56", "23:59:59"))
def test_canonical_declaring_entries_match_full_parse(packet, monkeypatch, timestamp):
    inputs, measure, _item = packet
    content = inputs.dailies[0].original_content
    expected = compile_pipeline_claims.declaring_entries(content, timestamp)
    with compile_pipeline_prompts.measure_choice_resolution(measure):
        measure.choice_resolver._immutable_byte_proof(content)
        parsing = Mock(wraps=evidence_resolver.daily_entries)
        point_compile_path(monkeypatch, "daily_entries", parsing)
        assert compile_pipeline_claims.declaring_entries(content, timestamp) == expected
        assert compile_pipeline_claims.declaring_entries(content, timestamp) == expected
    assert parsing.call_count == 0


def test_declaring_entries_parse_afresh_after_sizing(packet, monkeypatch):
    inputs, measure, _item = packet
    content = inputs.dailies[0].original_content
    with compile_pipeline_prompts.measure_choice_resolution(measure):
        expected = compile_pipeline_claims.declaring_entries(content, "12:34:56")
    parsing = Mock(wraps=evidence_resolver.daily_entries)
    point_compile_path(monkeypatch, "daily_entries", parsing)
    assert compile_pipeline_claims.declaring_entries(content, "12:34:56") == expected
    assert parsing.call_count == 1


def test_foreign_resolver_cannot_supply_canonical_entries(packet):
    inputs, _measure, _item = packet
    content = inputs.dailies[0].original_content
    expected = compile_pipeline_claims.declaring_entries(content, "12:34:56")
    forged = Mock()
    with compile_pipeline_prompts.source_choice_resolution(forged):
        assert compile_pipeline_claims.declaring_entries(content, "12:34:56") == expected
    forged.canonical_declaring_entries.assert_not_called()


def test_changed_immutable_bytes_require_a_new_canonical_parse(packet, monkeypatch):
    inputs, measure, _item = packet
    original = inputs.dailies[0].original_content
    changed = original + b"\nChanged source bytes.\n"
    expected = compile_pipeline_claims.declaring_entries(changed, "12:34:56")
    with compile_pipeline_prompts.measure_choice_resolution(measure):
        measure.choice_resolver._immutable_byte_proof(original)
        parsing = Mock(wraps=evidence_resolver.daily_entries)
        monkeypatch.setattr(evidence_resolver, "daily_entries", parsing)
        assert compile_pipeline_claims.declaring_entries(changed, "12:34:56") == expected
        assert compile_pipeline_claims.declaring_entries(changed, "12:34:56") == expected
    assert parsing.call_count == 1
