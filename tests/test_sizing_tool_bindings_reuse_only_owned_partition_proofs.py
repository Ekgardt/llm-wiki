"""Sizing reuses immutable partitions; accepted evidence still binds afresh."""
from dataclasses import replace
from unittest.mock import Mock

import compile_memory as compiler
import pytest
from evidence_resolver import EvidenceRef

from tests.test_compile_keeps_complete_tool_lines import _tool_parts


@pytest.fixture
def packet(tmp_path, monkeypatch):
    parts, raw = _tool_parts()
    monkeypatch.setattr(compiler, "ROOT", tmp_path)
    inputs = compiler.CompileInputs(tuple(parts), (), ())
    measure = compiler._ByteBatchMeasure(inputs)
    measure.partitions.for_part(parts[0])
    item = {"daily_date": "2026-10-02", "timestamp": "12:34:56",
            "quoted_text": raw.decode(), "claim": "Recorded tool target."}
    return inputs, measure, item


def test_sizing_does_not_reparse_a_joined_tool_day_for_each_binding(packet, monkeypatch):
    inputs, measure, item = packet
    expected = compiler._evidence_binding(item, inputs)
    parsing = Mock(wraps=compiler._partition_for_join)
    monkeypatch.setattr(compiler, "_partition_for_join", parsing)
    with compiler._measure_choice_resolution(measure):
        first = compiler._evidence_binding(item, inputs)
        second = compiler._evidence_binding(item, inputs)
    assert parsing.call_count == 0
    assert first == second == expected
    reference = EvidenceRef.parse(first["reference"])
    physical = inputs.dailies[0].original_content[reference.byte_start:reference.byte_end]
    assert physical == item["quoted_text"].encode()
    assert first["quote_sha256"] == compiler.sha256_bytes(physical)


def test_final_binding_reparses_after_sizing_scope_ends(packet, monkeypatch):
    inputs, measure, item = packet
    with compiler._measure_choice_resolution(measure):
        expected = compiler._evidence_binding(item, inputs)
    parsing = Mock(wraps=compiler._partition_for_join)
    monkeypatch.setattr(compiler, "_partition_for_join", parsing)
    assert compiler._evidence_binding(item, inputs) == expected
    assert parsing.call_count > 0
    assert compiler._SOURCE_CHOICE_PARSING.get() is None


@pytest.mark.parametrize("damage", ("content", "sha256", "part_index", "original_sha256"))
def test_reused_partitions_do_not_authorize_a_forged_member(packet, damage):
    inputs, measure, item = packet
    changes = {"content": b"forged", "sha256": "0" * 64,
               "part_index": inputs.dailies[-1].part_count,
               "original_sha256": "0" * 64}
    forged = replace(inputs.dailies[-1], **{damage: changes[damage]})
    changed = replace(inputs, dailies=(*inputs.dailies[:-1], forged))
    with compiler._measure_choice_resolution(measure), pytest.raises(ValueError):
        compiler._evidence_binding(item, changed)


def test_replaced_original_bytes_cannot_reuse_the_old_partition(packet):
    inputs, measure, item = packet
    changed = replace(inputs, dailies=tuple(
        replace(part, original_content=part.original_content + b"\n")
        for part in inputs.dailies))
    with compiler._measure_choice_resolution(measure), pytest.raises(ValueError):
        compiler._evidence_binding(item, changed)


def test_an_arbitrary_context_object_cannot_supply_partition_authority(packet):
    inputs, _measure, _item = packet
    part = inputs.dailies[0]
    expected = compiler._partition_for_join(part)
    forged = Mock()
    token = compiler._SOURCE_CHOICE_PARSING.set((None, forged))
    try:
        actual = compiler._join_partition(part, None)
    finally:
        compiler._SOURCE_CHOICE_PARSING.reset(token)
    assert actual == expected
    forged.for_part.assert_not_called()
