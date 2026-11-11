"""Native semantic compilation preserves physical proof across source parts."""
import json
from dataclasses import replace

import compile_pipeline
import compile_receipts
import pytest
from llm_wiki import (
    compile_pipeline_apply,
    compile_pipeline_base,
    compile_pipeline_batches,
    compile_pipeline_claims,
    compile_pipeline_operations,
    compile_pipeline_prompts,
    compile_pipeline_sources,
)

from tests.compile_paths import point_compile_path
from tests.compile_transactions_helpers import vault as vault
from tests.native_compile_uses_whole_container_helpers import (
    _apply_controlled_batch,
    _frame_covering_keys,
    _native_inputs,
    _native_operation,
    _part_with_key,
)


def test_long_native_frame_is_one_semantic_unit_with_all_original_parts(tmp_path, monkeypatch):
    prompt = "Начало. " * 2500 + "\nМой велосипед синий."
    inputs, _ = _native_inputs(tmp_path, monkeypatch, prompt)
    assert len(inputs.dailies) >= 2
    groups = compile_pipeline_batches.group_dailies(inputs, type("Budget", (), {"available_input_tokens": 10**6})(),
                                    lambda paths, *args: len(paths))
    raw = (tmp_path / inputs.dailies[0].logical_path).read_bytes()
    covering = _frame_covering_keys(inputs, raw)
    native_groups = [group for group in groups if group & covering]
    assert native_groups == [covering]
    subset = compile_pipeline_sources.subset_compile_inputs(inputs, native_groups[0])
    assert len(subset.sources) == 1
    assert "Мой велосипед синий." in compile_pipeline_prompts.draft_prompt(subset)
    operation = _native_operation(subset, "Мой велосипед синий.")
    record = compile_pipeline_operations.derived_claim(operation, operation["claims"][0], subset)
    assert record["schema_version"] == "claim/v2"
    raw = (tmp_path / subset.dailies[0].logical_path).read_bytes()
    from evidence_resolver import EvidenceRef
    reference = EvidenceRef.parse(record["evidence"]["reference"])
    assert raw[reference.byte_start:reference.byte_end].decode() == record["text"]
    assert json.loads(record["text"])["payload"]["prompt"] == prompt


def test_one_fragment_cannot_bind_whole_native_container(tmp_path, monkeypatch):
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "A" * 18000 + "\nTail.")
    start = inputs.dailies[0].original_content.index(b'    {"') + 4
    fragment = next(part for part in inputs.dailies if part.byte_start <= start < part.byte_end)
    one = compile_pipeline_sources.CompileInputs((fragment,), (), ())
    operation = _native_operation(inputs, "Tail.")
    with pytest.raises(ValueError, match="cover|complete|part"):
        compile_pipeline_operations.evidence_binding(operation["evidence"][0], one)


def test_complete_native_line_can_bind_without_model_copying_raw_json(tmp_path, monkeypatch):
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "First.\nTail.")
    operation = _native_operation(inputs, "Tail.")
    binding = compile_pipeline_operations.evidence_binding(operation["evidence"][0], inputs)
    quote = compile_pipeline_claims.verified_claim_quote(binding, inputs)
    assert json.loads(quote)["payload"]["prompt"] == "First.\nTail."


def test_atomic_native_unit_is_not_rejected_by_ordinary_packing_target(tmp_path, monkeypatch):
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "Начало. " * 2500 + "\nTail.")
    raw = inputs.dailies[0].original_content
    selected = compile_pipeline_sources.subset_compile_inputs(inputs, _frame_covering_keys(inputs, raw))
    target = compile_pipeline_base.compile_budget(None)
    batches = compile_pipeline_batches.pack_compile_batches(selected, model=None)
    assert len(batches) == 1
    batch = batches[0]
    assert batch.packing.measured_input_tokens > target.available_input_tokens
    assert batch.packing.max_input_tokens == (batch.packing.measured_input_tokens
                                            + target.reserved_output_tokens + target.safety_margin_tokens)
    assert compile_pipeline_prompts.compile_prompt_fits(compile_pipeline_prompts.draft_prompt(batch.inputs), system=compile_pipeline_base.DRAFT_SYSTEM,
                                        schema=compile_pipeline_base.RAW_PLAN_SCHEMA, model=None, token_adapters=None,
                                        budget=compile_pipeline_prompts.packing_budget(batch.packing, None))


@pytest.mark.parametrize("change", [{"line_index": 0}, {"byte_start": 0}, {"line_index": True}])
def test_native_selector_cannot_point_at_other_line_or_container(tmp_path, monkeypatch, change):
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "First.\nTail.")
    operation = _native_operation(inputs, "Tail.")
    operation["evidence"][0]["native_event"].update(change)
    with pytest.raises(ValueError):
        compile_pipeline_operations.evidence_binding(operation["evidence"][0], inputs)


def test_native_publication_commits_every_covering_part_and_exact_v2_ledger(vault, monkeypatch):
    from claims import parse_claim_ledger
    from markdown_transaction import MarkdownCoordinator

    root, state_root = vault
    inputs, _ = _native_inputs(root, monkeypatch, "A" * 18000 + "\nМой велосипед синий.")
    raw = (root / inputs.dailies[0].logical_path).read_bytes()
    covering = _frame_covering_keys(inputs, raw)
    selected = compile_pipeline_sources.subset_compile_inputs(inputs, covering)
    batch = compile_pipeline_batches.pack_compile_batches(selected, model=None)[0]
    operation = _native_operation(batch.inputs, "Мой велосипед синий.")
    derived = compile_pipeline_operations.with_derived_claims([operation], batch.inputs)
    plan = compile_pipeline_prompts.normalize_plan(derived, batch.inputs)
    coordinator = MarkdownCoordinator(root, state_root)
    result = compile_pipeline_apply.apply_compile_plan(batch.inputs, plan, action_key="a" * 64,
                                         trigger="manual", coordinator=coordinator, batch=batch,
                                         provider_budget={"provider": "fake", "model": "fake-v1",
                                                          "max_output_tokens": 4000})
    assert result.state == "committed"
    page = root / "knowledge/notes/bounded-lease-expiry.md"
    ledger = parse_claim_ledger(page.read_bytes())
    assert ledger["schema_version"] == "claim-ledger/v2"
    selector = compile_receipts.receipt_predicate(coordinator)
    receipts = [selector.receipt(part) for part in batch.inputs.dailies]
    assert len(receipts) == len(covering)
    assert all(receipt is not None and receipt["evidence"] for receipt in receipts)
    assert len({receipt["operation_id"] for receipt in receipts}) == 1


def _unit_with_part(units, selected):
    return next(unit for unit in units if selected in _unit_keys(unit))


def _unit_keys(unit):
    return {part.part_key for part in unit}


def test_precompiled_companion_is_context_and_receipt_is_preserved(tmp_path, monkeypatch):
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "A" * 18000 + "\nTail.")
    parts = list(inputs.dailies)
    raw = parts[0].original_content
    keys = _frame_covering_keys(inputs, raw)
    first = _part_with_key(parts, keys)
    selected = compile_pipeline_sources.daily_parts(first.logical_path, raw,
                                     lambda path, digest: digest == first.sha256,
                                     native_frames=first.native_frames)
    unit = _unit_with_part(compile_pipeline_sources.native_part_units(selected), first.part_key)
    assert len(unit) == len(keys)
    assert sum(part.already_compiled for part in unit) == 1
    pending = compile_pipeline_sources.pending_daily_parts(compile_pipeline_sources.CompileInputs(tuple(unit), (), ()))
    assert first.part_key not in _unit_keys(pending)


def test_native_critic_sees_exact_nfd_user_line(tmp_path, monkeypatch):
    quote = "Cafe\u0301 bicycle."
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "First.\nCafe bicycle.")
    operation = _native_operation(inputs, quote)
    # Isolate serialization after byte verification. The legacy breadcrumb
    # producer cannot encode NFD verbatim; this test does not claim it can.
    binding = {"source_path": inputs.dailies[0].logical_path,
               "source_digest": "a" * 64, "quote_sha256": "b" * 64}
    point_compile_path(monkeypatch, "validate_semantic_operation", lambda operation, inputs: (operation, [binding]))
    request = compile_pipeline_prompts.critique_prompt(inputs, [operation])
    cited = json.loads(request.split("CITED EVIDENCE\n", 1)[1])
    assert cited[0]["quoted_text"].encode() == quote.encode()
    normalized = json.loads(request.split("OPERATIONS\n", 1)[1].split("CITED EVIDENCE", 1)[0])
    assert normalized[0]["evidence"][0]["quoted_text"].encode() == quote.encode()


@pytest.mark.parametrize("changes", [
    {"text": "First.\nFabricated bicycle ownership."},
    {"encoded": '{"fabricated":true}'},
    {"byte_end": 123},
    {"source_path": "knowledge/daily/other.md"},
])
def test_native_binding_rejects_cached_frame_tampering(tmp_path, monkeypatch, changes):
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "First.\nActual observation.")
    frame = inputs.dailies[0].native_frames[0]
    changed = replace(frame, **changes)
    parts = tuple(replace(part, native_frames=(changed,)) for part in inputs.dailies)
    forged = replace(inputs, dailies=parts)
    operation = _native_operation(forged, changes.get("text", frame.text).splitlines()[1])
    with pytest.raises(ValueError, match="native.*(cache|canonical|source|container|proof)"):
        compile_pipeline_operations.evidence_binding(operation["evidence"][0], forged)


def _native_with_committed_companion(vault, monkeypatch):
    from markdown_transaction import MarkdownCoordinator

    root, state_root = vault
    inputs, _ = _native_inputs(root, monkeypatch, "A" * 18000 + "\nМой велосипед синий.")
    first = _part_with_key(inputs.dailies, _frame_covering_keys(inputs, inputs.dailies[0].original_content))
    historical = replace(inputs, dailies=(replace(first, native_frames=()),), sources=())
    old_batch = compile_pipeline_batches.pack_compile_batches(historical, model=None)[0]
    coordinator = MarkdownCoordinator(root, state_root)
    result = _apply_controlled_batch(old_batch, compile_pipeline_prompts.normalize_plan([], old_batch.inputs),
                                    coordinator, "a" * 64)
    assert result.state == "committed"
    selector = compile_receipts.receipt_predicate(coordinator)
    old_receipt = selector.receipt(first)
    old_path = root / f"knowledge/daily/receipts/v4-{old_receipt['source_identity']}.md"
    old_bytes = old_path.read_bytes()
    current = compile_pipeline_sources.snapshot_compile_inputs([root / first.logical_path], compiled=selector)
    keys = _frame_covering_keys(current, first.original_content)
    batch = compile_pipeline_batches.pack_compile_batches(compile_pipeline_sources.subset_compile_inputs(current, keys), model=None)[0]
    operation = _native_operation(batch.inputs, "Мой велосипед синий.")
    plan = compile_pipeline_prompts.normalize_plan(compile_pipeline_operations.with_derived_claims([operation], batch.inputs), batch.inputs)
    return coordinator, batch, plan, old_path, old_bytes


def test_actual_committed_companion_receipt_is_preserved(vault, monkeypatch):
    coordinator, batch, plan, old_path, old_bytes = _native_with_committed_companion(vault, monkeypatch)
    result = _apply_controlled_batch(batch, plan, coordinator, "b" * 64)
    assert result.state == "committed"
    assert old_path.read_bytes() == old_bytes
    receipts = [compile_receipts.receipt_predicate(coordinator).receipt(part) for part in batch.inputs.dailies]
    assert all(receipts)
    assert len({receipt["operation_id"] for receipt in receipts}) == 2


def test_missing_committed_companion_blocks_native_publication(vault, monkeypatch):
    coordinator, batch, plan, old_path, _ = _native_with_committed_companion(vault, monkeypatch)
    old_path.unlink()
    with pytest.raises(ValueError, match="companion.*receipt|receipt.*companion"):
        _apply_controlled_batch(batch, plan, coordinator, "b" * 64)


def test_native_receipt_rejects_reversed_same_source_part_order(vault, monkeypatch):
    coordinator, batch, plan, _, _ = _native_with_committed_companion(vault, monkeypatch)
    _apply_controlled_batch(batch, plan, coordinator, "b" * 64)
    part = compile_pipeline_sources.pending_daily_parts(batch.inputs)[0]
    record = compile_receipts.receipt_predicate(coordinator).receipt(part)
    record["batch_manifest"].reverse()
    record["batch_manifest_sha256"] = compile_pipeline.sha256_bytes(compile_pipeline.canonical_json_bytes(record["batch_manifest"]))
    with pytest.raises(ValueError, match="manifest is not sorted"):
        from llm_wiki.formats.receipts import require_v4_manifest
        require_v4_manifest(record)


def test_companion_receipt_hash_is_checked_at_transaction_prepare(vault, monkeypatch):
    coordinator, batch, plan, old_path, old_bytes = _native_with_committed_companion(vault, monkeypatch)
    original_prepare = coordinator.prepare

    def external_edit_before_prepare(*args, **kwargs):
        relative = old_path.relative_to(coordinator.vault).as_posix()
        assert kwargs["preconditions"][relative] == compile_pipeline.sha256_bytes(old_bytes)
        old_path.write_bytes(old_bytes + b"external damage\n")
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(coordinator, "prepare", external_edit_before_prepare)
    with pytest.raises(ValueError):
        _apply_controlled_batch(batch, plan, coordinator, "b" * 64)
    assert not (old_path.parents[2] / "notes/bounded-lease-expiry.md").exists()
