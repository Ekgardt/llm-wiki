from __future__ import annotations

import json
from dataclasses import replace

import compile_memory as compiler
import pytest
from evidence_resolver import EvidenceRef, EvidenceResolver
from markdown_transaction import MarkdownCoordinator
from reliable_memory import canonical_json_bytes, sha256_bytes

from tests.test_compile_transactions import _semantic_plan
from tests.test_compile_transactions import vault as vault


def _continuation():
    prefix = b"## [10:00:00] session-end | manual\n" + b"An earlier observation.\n" * 900
    quote = "The continuation preserves the original observation clock."
    content = prefix + quote.encode() + b"\n"
    parts = compiler._daily_parts("knowledge/daily/2026-10-03.md", content)
    selected = next(part for part in parts if quote.encode() in part.content)
    inputs = compiler.CompileInputs(
        (selected,),
        (compiler.SourceSnapshot(selected.logical_path, selected.content, selected.sha256),),
        (),
    )
    evidence = {
        "daily_date": "2026-10-03",
        "timestamp": "10:00:00",
        "quoted_text": quote,
        "claim": "Continuation evidence retains its clock.",
    }
    return content, selected, inputs, evidence


def test_a_continuation_binds_to_exact_original_bytes():
    content, selected, inputs, evidence = _continuation()
    binding = compiler._evidence_binding(evidence, inputs)
    reference = EvidenceRef.parse(binding["reference"])
    resolved = EvidenceResolver(compiler.ROOT).resolve_bytes(
        reference, content, source_path=compiler.ROOT / selected.logical_path
    )
    assert reference.source_sha256 == sha256_bytes(content)
    assert selected.byte_start <= reference.byte_start < reference.byte_end <= selected.byte_end
    assert resolved.bytes.decode() == evidence["quoted_text"]
    assert binding["source_digest"] == selected.sha256
    assert compiler._verified_claim_quote(binding, inputs) == evidence["quoted_text"]


def test_a_reference_cannot_reach_another_work_part():
    content, selected, inputs, _evidence = _continuation()
    reference = EvidenceRef("2026-10-03", sha256_bytes(content), "10:00:00", 40, 45)
    assert reference.byte_end < selected.byte_start
    with pytest.raises(ValueError, match="absent from the snapshot"):
        compiler._reference_source(inputs, reference)


def test_a_split_physical_line_is_not_published_as_a_complete_line():
    content = b"## [10:00:00] session-end | manual\n" + b"x" * 20000 + b"\n"
    parts = compiler._daily_parts("knowledge/daily/2026-10-03.md", content)
    selected = parts[-1]
    inputs = compiler.CompileInputs((selected,), (), ())
    evidence = {
        "daily_date": "2026-10-03",
        "timestamp": "10:00:00",
        "quoted_text": selected.content.decode().strip(),
        "claim": "A truncated line cannot prove a complete claim.",
    }
    with pytest.raises(ValueError, match="complete physical source line"):
        compiler._evidence_binding(evidence, inputs)


def test_the_original_context_changes_cache_identity():
    content, selected, inputs, _evidence = _continuation()
    changed = content.replace(b"10:00:00", b"11:00:00", 1)
    changed_parts = compiler._daily_parts(selected.logical_path, changed)
    changed_part = changed_parts[selected.part_index]
    assert changed_part.sha256 == selected.sha256
    changed_inputs = compiler.CompileInputs((changed_part,), inputs.sources, ())
    assert compiler._entry_context_identity(inputs) != compiler._entry_context_identity(changed_inputs)


def test_a_continuation_reference_survives_later_appends(tmp_path):
    content, selected, inputs, evidence = _continuation()
    path = tmp_path / selected.logical_path
    path.parent.mkdir(parents=True)
    path.write_bytes(content + b"\n## [11:00:00] session-end | manual\nA later entry.\n")
    binding = compiler._evidence_binding(evidence, inputs)
    resolved = EvidenceResolver(tmp_path).resolve(binding["reference"])
    assert resolved.bytes.decode() == evidence["quoted_text"]


def _published_continuation(vault):
    root, state_root = vault
    content, _selected, _inputs, evidence = _continuation()
    path = root / "knowledge/daily/2026-07-14.md"
    path.write_bytes(content)
    inputs = compiler.snapshot_compile_inputs([path])
    batches = compiler.pack_compile_batches(inputs, model="fake-v1")
    batch = next(item for item in batches if evidence["quoted_text"].encode() in item.inputs.dailies[0].content)
    plan = _semantic_plan()
    operation = json.loads(plan["operations"][0]["content"])
    operation["evidence"][0]["quoted_text"] = evidence["quoted_text"]
    operation["body_markdown"] = evidence["quoted_text"]
    plan["operations"][0]["content"] = canonical_json_bytes(operation).decode()
    coordinator = MarkdownCoordinator(root, state_root)
    result = compiler.apply_compile_plan(
        batch.inputs, plan, action_key="c" * 64, trigger="manual",
        coordinator=coordinator, completed_at="2026-07-14T12:00:00Z", batch=batch,
        provider_budget={"provider": "fake", "model": "fake-v1", "max_output_tokens": 4000},
    )
    part = batch.inputs.dailies[0]
    receipt = compiler._read_snapshot_receipt(part, coordinator)
    assert receipt is not None and receipt["operation_id"] == result.operation_id
    page = root / "knowledge/notes/exact-byte-pattern.md"
    assert evidence["quoted_text"] in page.read_text()
    reference = compiler._evidence_binding(operation["evidence"][0], batch.inputs)["reference"]
    assert EvidenceResolver(root).resolve(reference).bytes.decode() == evidence["quoted_text"]
    return path, coordinator, part


def test_a_continuation_publishes_with_its_own_v4_part_receipt(vault):
    _published_continuation(vault)


def test_an_old_receipt_cannot_hide_a_changed_original_clock(vault):
    path, coordinator, part = _published_continuation(vault)
    changed = path.read_bytes().replace(b"10:00:00", b"11:00:00", 1)
    path.write_bytes(changed)
    retained = compiler._read_snapshot_receipt(part, coordinator)
    assert retained is not None
    pending = compiler._daily_parts(part.logical_path, changed, compiler._receipt_predicate(coordinator))
    assert any(item.sha256 == part.sha256 for item in pending)


def test_a_continuation_prompt_names_the_actual_entry_clock():
    _content, _selected, inputs, _evidence = _continuation()
    prompt = compiler._draft_prompt(inputs)
    assert "10:00:00" in prompt


@pytest.mark.parametrize("field", ["sha256", "original_sha256"])
def test_a_forged_snapshot_hash_cannot_create_a_context_receipt(field):
    _content, part, _inputs, _evidence = _continuation()
    forged = replace(part, **{field: "0" * 64})
    with pytest.raises(ValueError, match="digest"):
        compiler._v4_source_descriptor(forged)


def test_a_changed_original_body_cannot_retain_the_old_digest():
    content, part, _inputs, _evidence = _continuation()
    forged = replace(part, original_content=content.replace(b"10:00:00", b"11:00:00", 1))
    with pytest.raises(ValueError, match="digest"):
        compiler._v4_source_descriptor(forged)


def test_a_part_cannot_claim_another_absolute_slice():
    _content, part, _inputs, _evidence = _continuation()
    forged = replace(part, byte_start=part.byte_start - 1, byte_end=part.byte_end - 1)
    with pytest.raises(ValueError, match="slice"):
        compiler._v4_source_descriptor(forged)


@pytest.mark.parametrize("record", [[], None, 7, "scalar"])
def test_a_non_object_receipt_has_a_standard_visible_refusal(record):
    raw = b"```json\n" + json.dumps(record).encode() + b"\n```"
    with pytest.raises(ValueError, match="receipt"):
        compiler.parse_compile_receipt_version(
            raw, logical_path="knowledge/daily/2026-10-03.md", source_sha256="0" * 64
        )


def test_a_pure_append_reuses_the_saved_original_prefix(vault):
    path, coordinator, part = _published_continuation(vault)
    appended = path.read_bytes() + b"## [12:00:00] session-end | manual\n" + b"A later observation.\n" * 1000
    path.write_bytes(appended)
    current = compiler._daily_parts(part.logical_path, appended)
    same_part = next(item for item in current if item.sha256 == part.sha256)
    assert same_part.original_sha256 != part.original_sha256
    selection = compiler._receipt_predicate(coordinator)
    assert selection.matches(same_part)
    pending = compiler._daily_parts(part.logical_path, appended, compiled=selection)
    assert all(item.sha256 != part.sha256 for item in pending)


def test_an_empty_source_requires_a_real_context_receipt(vault):
    root, state_root = vault
    coordinator = MarkdownCoordinator(root, state_root)
    logical = "knowledge/daily/2026-10-03.md"
    part = compiler._daily_parts(logical, b"")[0]
    descriptor = compiler._v4_source_descriptor(part)
    assert descriptor["byte_start"] == descriptor["byte_end"] == 0
    assert descriptor["original_byte_size"] == descriptor["byte_size"] == 0
    assert not compiler.daily_is_compiled(logical, b"", compiler._receipt_predicate(coordinator))
