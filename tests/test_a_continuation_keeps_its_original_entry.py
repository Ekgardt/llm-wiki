from __future__ import annotations

import json
from dataclasses import replace

import compile_pipeline
import compile_receipts
import pytest
from evidence_resolver import EvidenceRef, EvidenceResolver
from llm_wiki import (
    compile_pipeline_claims,
    compile_pipeline_operations,
    compile_pipeline_prompts,
    compile_pipeline_receipts,
    compile_pipeline_sources,
    runtime_roots,
)
from markdown_transaction import MarkdownCoordinator
from reliable_memory import sha256_bytes

from tests.a_continuation_keeps_its_original_entry_helpers import (
    _continuation,
    _published_continuation,
)
from tests.compile_transactions_helpers import vault as vault


def test_a_continuation_binds_to_exact_original_bytes():
    content, selected, inputs, evidence = _continuation()
    binding = compile_pipeline_operations.evidence_binding(evidence, inputs)
    reference = EvidenceRef.parse(binding["reference"])
    resolved = EvidenceResolver(runtime_roots.vault_root()).resolve_bytes(
        reference, content, source_path=runtime_roots.vault_root() / selected.logical_path
    )
    assert reference.source_sha256 == sha256_bytes(content)
    assert selected.byte_start <= reference.byte_start < reference.byte_end <= selected.byte_end
    assert resolved.bytes.decode() == evidence["quoted_text"]
    assert binding["source_digest"] == selected.sha256
    assert compile_pipeline_claims.verified_claim_quote(binding, inputs) == evidence["quoted_text"]


def test_a_reference_cannot_reach_another_work_part():
    content, selected, inputs, _evidence = _continuation()
    reference = EvidenceRef("2026-10-03", sha256_bytes(content), "10:00:00", 40, 45)
    assert reference.byte_end < selected.byte_start
    with pytest.raises(ValueError, match="absent from the snapshot"):
        compile_pipeline_claims.reference_source(inputs, reference)


def test_a_split_physical_line_is_not_published_as_a_complete_line():
    content = b"## [10:00:00] session-end | manual\n" + b"x" * 20000 + b"\n"
    parts = compile_pipeline_sources.daily_parts("knowledge/daily/2026-10-03.md", content)
    selected = parts[-1]
    inputs = compile_pipeline_sources.CompileInputs((selected,), (), ())
    evidence = {
        "daily_date": "2026-10-03",
        "timestamp": "10:00:00",
        "quoted_text": selected.content.decode().strip(),
        "claim": "A truncated line cannot prove a complete claim.",
    }
    with pytest.raises(ValueError, match="complete physical source line"):
        compile_pipeline_operations.evidence_binding(evidence, inputs)


def test_the_original_context_changes_cache_identity():
    content, selected, inputs, _evidence = _continuation()
    changed = content.replace(b"10:00:00", b"11:00:00", 1)
    changed_parts = compile_pipeline_sources.daily_parts(selected.logical_path, changed)
    changed_part = changed_parts[selected.part_index]
    assert changed_part.sha256 == selected.sha256
    changed_inputs = compile_pipeline_sources.CompileInputs((changed_part,), inputs.sources, ())
    assert compile_pipeline_prompts.entry_context_identity(inputs) != compile_pipeline_prompts.entry_context_identity(changed_inputs)


def test_a_continuation_reference_survives_later_appends(tmp_path):
    content, selected, inputs, evidence = _continuation()
    path = tmp_path / selected.logical_path
    path.parent.mkdir(parents=True)
    path.write_bytes(content + b"\n## [11:00:00] session-end | manual\nA later entry.\n")
    binding = compile_pipeline_operations.evidence_binding(evidence, inputs)
    resolved = EvidenceResolver(tmp_path).resolve(binding["reference"])
    assert resolved.bytes.decode() == evidence["quoted_text"]


def test_a_continuation_publishes_with_its_own_v4_part_receipt(vault):
    _published_continuation(vault)


def test_an_old_receipt_cannot_hide_a_changed_original_clock(vault):
    path, coordinator, part = _published_continuation(vault)
    changed = path.read_bytes().replace(b"10:00:00", b"11:00:00", 1)
    path.write_bytes(changed)
    retained = compile_pipeline_receipts.read_snapshot_receipt(part, coordinator)
    assert retained is not None
    pending = compile_pipeline_sources.daily_parts(part.logical_path, changed, compile_receipts.receipt_predicate(coordinator))
    assert any(item.sha256 == part.sha256 for item in pending)


def test_a_continuation_prompt_names_the_actual_entry_clock():
    _content, _selected, inputs, _evidence = _continuation()
    prompt = compile_pipeline_prompts.draft_prompt(inputs)
    assert "10:00:00" in prompt


@pytest.mark.parametrize("field", ["sha256", "original_sha256"])
def test_a_forged_snapshot_hash_cannot_create_a_context_receipt(field):
    _content, part, _inputs, _evidence = _continuation()
    forged = replace(part, **{field: "0" * 64})
    with pytest.raises(ValueError, match="digest"):
        compile_pipeline_receipts.v4_source_descriptor(forged)


def test_a_changed_original_body_cannot_retain_the_old_digest():
    content, part, _inputs, _evidence = _continuation()
    forged = replace(part, original_content=content.replace(b"10:00:00", b"11:00:00", 1))
    with pytest.raises(ValueError, match="digest"):
        compile_pipeline_receipts.v4_source_descriptor(forged)


def test_a_part_cannot_claim_another_absolute_slice():
    _content, part, _inputs, _evidence = _continuation()
    forged = replace(part, byte_start=part.byte_start - 1, byte_end=part.byte_end - 1)
    with pytest.raises(ValueError, match="slice"):
        compile_pipeline_receipts.v4_source_descriptor(forged)


@pytest.mark.parametrize("record", [[], None, 7, "scalar"])
def test_a_non_object_receipt_has_a_standard_visible_refusal(record):
    raw = b"```json\n" + json.dumps(record).encode() + b"\n```"
    with pytest.raises(ValueError, match="receipt"):
        compile_pipeline.parse_compile_receipt_version(
            raw, logical_path="knowledge/daily/2026-10-03.md", source_sha256="0" * 64
        )


def test_a_pure_append_reuses_the_saved_original_prefix(vault):
    path, coordinator, part = _published_continuation(vault)
    appended = path.read_bytes() + b"## [12:00:00] session-end | manual\n" + b"A later observation.\n" * 1000
    path.write_bytes(appended)
    current = compile_pipeline_sources.daily_parts(part.logical_path, appended)
    same_part = next(item for item in current if item.sha256 == part.sha256)
    assert same_part.original_sha256 != part.original_sha256
    selection = compile_receipts.receipt_predicate(coordinator)
    assert selection.matches(same_part)
    pending = compile_pipeline_sources.daily_parts(part.logical_path, appended, compiled=selection)
    assert all(item.sha256 != part.sha256 for item in pending)


def test_an_empty_source_requires_a_real_context_receipt(vault):
    root, state_root = vault
    coordinator = MarkdownCoordinator(root, state_root)
    logical = "knowledge/daily/2026-10-03.md"
    part = compile_pipeline_sources.daily_parts(logical, b"")[0]
    descriptor = compile_pipeline_receipts.v4_source_descriptor(part)
    assert descriptor["byte_start"] == descriptor["byte_end"] == 0
    assert descriptor["original_byte_size"] == descriptor["byte_size"] == 0
    assert not compile_pipeline.daily_is_compiled(logical, b"", compile_receipts.receipt_predicate(coordinator))
