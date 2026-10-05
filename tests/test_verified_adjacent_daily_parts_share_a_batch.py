"""Only canonical complete adjacent parts can share a physical day source."""
from dataclasses import replace
from types import SimpleNamespace

import compile_memory as compiler
import pytest


def _parts():
    content = b"# 2026-09-01\n\n## 10:00\n" + b"- complete ordinary line\n" * 1600
    return compiler._daily_parts("knowledge/daily/2026-09-01.md", content)


def _groups(parts):
    inputs = compiler.CompileInputs(tuple(parts), (), ())
    budget = SimpleNamespace(available_input_tokens=1_000_000)
    return compiler._group_dailies(inputs, budget, compiler._ByteBatchMeasure(inputs))


def test_verified_adjacent_parts_join_without_losing_bytes():
    parts = _parts()
    assert len(parts) > 1
    assert len(_groups(parts)) == 1
    sources = compiler._deduplicated_sources(parts)
    assert len(sources) == 1
    assert sources[0].content == parts[0].original_content
    assert compiler._packing_algorithm(SimpleNamespace(dailies=parts)) == "compile-complete-items/v2"


@pytest.mark.parametrize("field,value", [("original_sha256", "0" * 64), ("sha256", "0" * 64),
                                         ("byte_start", 0), ("part_count", 999),
                                         ("content", b"forged"), ("original_content", bytearray(b"mutable"))])
def test_forged_part_context_cannot_join(field, value):
    parts = _parts()
    forged = [parts[0], replace(parts[1], **{field: value})]
    assert len(_groups(forged)) == 2
    with pytest.raises(ValueError):
        compiler._deduplicated_sources(forged)


def test_missing_partition_member_cannot_join():
    parts = _parts()
    assert len(parts) >= 3
    selected = [parts[0], parts[2]]
    assert len(_groups(selected)) == 2
    with pytest.raises(ValueError):
        compiler._deduplicated_sources(selected)


def test_existing_budget_still_splits_verified_parts():
    parts = _parts()
    budget = SimpleNamespace(available_input_tokens=1)
    inputs = SimpleNamespace(dailies=parts)
    groups = compiler._group_dailies(inputs, budget, lambda paths: len(paths))
    assert [len(group) for group in groups] == [1] * len(parts)


def test_v4_manifest_retains_every_joined_part_and_v1_stays_strict():
    parts = _parts()
    inputs = SimpleNamespace(dailies=parts)
    manifest = compiler._v4_manifest(inputs)
    assert len(manifest) == len(parts)
    assert [item["byte_start"] for item in manifest] == [part.byte_start for part in parts]
    with pytest.raises(ValueError):
        compiler._require_packing_manifest_version({"packing": {"algorithm": "compile-complete-items/v1"}}, manifest)


def test_partition_memo_parses_original_once_but_final_validation_is_fresh(monkeypatch):
    original = compiler._partition_for_join
    calls = []

    def observed(part, **kwargs):
        calls.append(part.original_sha256)
        return original(part, **kwargs)

    monkeypatch.setattr(compiler, "_partition_for_join", observed)
    parts = _parts()
    assert len(_groups(parts)) == 1
    assert len(calls) == 1
    compiler._deduplicated_sources(parts)
    assert len(calls) == 2


def test_native_frame_and_its_verified_neighbor_share_actual_byte_measure(tmp_path, monkeypatch):
    from tests.test_native_compile_uses_whole_container import _native_inputs

    inputs, _ = _native_inputs(tmp_path, monkeypatch, "Мой велосипед синий. " * 600)
    assert len(inputs.dailies) > 1
    budget = SimpleNamespace(available_input_tokens=1_000_000)
    opaque = compiler._group_dailies(inputs, budget, lambda paths: len(paths))
    assert len(opaque) > 1
    actual = compiler._group_dailies(inputs, budget, compiler._ByteBatchMeasure(inputs))
    assert len(actual) == 1
    selected = compiler._subset_compile_inputs(inputs, actual[0])
    assert selected.sources[0].content == inputs.dailies[0].original_content
    assert "Мой велосипед синий." in selected.sources[0].prompt_content.decode()


def test_measure_bound_to_different_inputs_cannot_authorize_join():
    parts = _parts()
    owner = compiler.CompileInputs(tuple(parts), (), ())
    foreign = compiler.CompileInputs(tuple(parts), (), ())
    budget = SimpleNamespace(available_input_tokens=1_000_000)
    assert len(compiler._group_dailies(foreign, budget, compiler._ByteBatchMeasure(owner))) == len(parts)


def test_missing_native_cover_is_refused(tmp_path, monkeypatch):
    from tests.test_native_compile_uses_whole_container import _native_inputs

    inputs, _ = _native_inputs(tmp_path, monkeypatch, "Полная строка. " * 3000)
    assert len(inputs.dailies) > 2
    with pytest.raises(ValueError, match="all covering source parts"):
        compiler._deduplicated_sources(inputs.dailies[:2])


def test_actual_adopted_publication_keeps_each_ordinary_part_receipt(tmp_path, monkeypatch):
    from tests.test_native_compile_companion_adopted import _adopted_environment
    from tests.test_native_compile_uses_whole_container import _apply_controlled_batch

    root, coordinator = _adopted_environment(tmp_path, monkeypatch)
    parts = _parts()
    target = root / parts[0].logical_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(parts[0].original_content)
    selected = compiler.CompileInputs(tuple(parts[:2]), (), ())
    batch = compiler._compile_batch(selected, {part.part_key for part in selected.dailies},
                                    compiler._compile_budget(None), None, None, optional_paths=set())
    result = _apply_controlled_batch(batch, {"schema_version": "compile-plan/v2", "operations": []},
                                     coordinator, compiler.sha256_bytes(b"verified-ordinary-adjacent"))
    assert result.state == "committed"
    receipts = list((root / "knowledge/daily/receipts").glob("*.md"))
    assert len(receipts) == len(selected.dailies)
    selection = compiler._receipt_predicate(coordinator)
    parsed = [selection.receipt(part) for part in selected.dailies]
    assert {item["source"]["sha256"] for item in parsed} == {part.sha256 for part in selected.dailies}


def test_source_bound_tokenizer_preserves_explicit_adapter_and_full_bytes():
    parts = _parts()
    inputs = compiler.CompileInputs(tuple(parts), (), ())
    adapters = {"controlled-tokenizer": lambda text: len(text.encode("utf-8"))}
    measure = compiler._batch_measure(inputs, "controlled-tokenizer", adapters)
    budget = SimpleNamespace(available_input_tokens=1_000_000)
    groups = compiler._group_dailies(inputs, budget, measure)
    assert len(groups) == 1
    selected = compiler._subset_compile_inputs(inputs, groups[0])
    assert measure(groups[0]) == len(compiler._draft_prompt_text(selected).encode("utf-8"))
    assert selected.sources[0].content == parts[0].original_content


def test_join_preserves_unicode_physical_bytes_without_normalization():
    original = b"# 2026-09-01\n\n## 10:00\n" + ("- e\u0301 и машина.\n" * 2000).encode()
    parts = compiler._daily_parts("knowledge/daily/2026-09-01.md", original)
    joined = compiler._deduplicated_sources(parts)
    assert joined[0].content == original
    assert joined[0].sha256 == compiler.sha256_bytes(original)


def test_equal_part_digests_remain_distinct_v4_physical_contexts():
    original = b"# 2026-09-01\n\n## 10:00\n" + b"x\n" * 40000
    parts = compiler._daily_parts("knowledge/daily/2026-09-01.md", original)
    manifest = compiler._v4_manifest(compiler.CompileInputs(tuple(parts), (), ()))
    assert len({part.sha256 for part in parts}) < len(parts)
    assert len({compiler.compile_context_source_identity(item) for item in manifest}) == len(parts)
    assert compiler._deduplicated_sources(parts)[0].content == original


def test_removed_native_cache_cannot_hide_missing_physical_companion(tmp_path, monkeypatch):
    from tests.test_native_compile_uses_whole_container import _native_inputs

    inputs, _ = _native_inputs(tmp_path, monkeypatch, "Полная строка. " * 3000)
    selected = [replace(part, native_frames=()) for part in inputs.dailies[:2]]
    with pytest.raises(ValueError, match="covering source parts"):
        compiler._deduplicated_sources(selected)


def test_different_original_physical_versions_do_not_join():
    parts = _parts()
    other = parts[0].original_content + b"another source version\n"
    changed = replace(parts[1], original_content=other, original_sha256=compiler.sha256_bytes(other))
    selected = [parts[0], changed]
    assert len(_groups(selected)) == 2
    with pytest.raises(ValueError, match="different physical sources"):
        compiler._deduplicated_sources(selected)


def test_adopted_equal_digest_parts_keep_independent_context_receipts(tmp_path, monkeypatch):
    from tests.test_native_compile_companion_adopted import _adopted_environment
    from tests.test_native_compile_uses_whole_container import _apply_controlled_batch

    root, coordinator = _adopted_environment(tmp_path, monkeypatch)
    original = b"# 2026-09-01\n\n## 10:00\n" + b"x\n" * 40000
    parts = compiler._daily_parts("knowledge/daily/2026-09-01.md", original)
    target = root / parts[0].logical_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(original)
    inputs = compiler.CompileInputs(tuple(parts), (), ())
    defaults = compiler._compile_budget(None)
    keys = {part.part_key for part in parts}
    measured = compiler._ByteBatchMeasure(inputs)(keys)
    budget = compiler.ContextBudget(None, measured + defaults.reserved_output_tokens + defaults.safety_margin_tokens,
                                    defaults.reserved_output_tokens, defaults.safety_margin_tokens)
    batch = compiler._compile_batch(inputs, keys, budget, None, None, optional_paths=set())
    result = _apply_controlled_batch(batch, {"schema_version": "compile-plan/v2", "operations": []},
                                     coordinator, compiler.sha256_bytes(b"equal-digest-distinct-contexts"))
    assert result.state == "committed"
    selection = compiler._receipt_predicate(coordinator)
    receipts = [selection.receipt(part) for part in parts]
    assert len({item["source_identity"] for item in receipts}) == len(parts)
    assert {item["operation_id"] for item in receipts} == {result.operation_id}


def _observe_projection_lifetimes(monkeypatch):
    import weakref

    references = []
    original = compiler._native_unit_source

    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        references.append(weakref.ref(result))
        return result

    monkeypatch.setattr(compiler, "_native_unit_source", observed)
    return references


def test_rejected_joined_candidates_do_not_remain_owned(monkeypatch):
    inputs = compiler.CompileInputs(tuple(_parts()), (), ())
    references = _observe_projection_lifetimes(monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    groups = compiler._group_dailies(inputs, compiler._compile_budget(None), measure)
    assert len(groups) > 1
    live = [reference() for reference in references if reference() is not None]
    assert len(live) <= len(measure.projection_sources)


def test_final_packing_reuses_only_immutable_partition_proofs(monkeypatch):
    inputs = compiler.CompileInputs(tuple(_parts()), (), ())
    calls = []
    original = compiler._partition_for_join

    def observed(part, **kwargs):
        calls.append(part.original_content)
        return original(part, **kwargs)

    monkeypatch.setattr(compiler, "_partition_for_join", observed)
    batches = compiler.pack_compile_batches(inputs, model=None)
    assert len(batches) > 1
    assert len(calls) == 1
    assert calls[0] is inputs.dailies[0].original_content


def test_final_partition_memo_does_not_reuse_native_head_authority(tmp_path, monkeypatch):
    from tests.test_compile_native_journal_parse_belongs_to_the_immutable_source import _two_units
    inputs, keys = _two_units(tmp_path, monkeypatch)
    measure = compiler._ByteBatchMeasure(inputs)
    measure(keys[0])
    head = next(tmp_path.glob("knowledge/raw/sessions/**/*.breadcrumb.md"))
    head.write_bytes(head.read_bytes() + b"changed\n")
    with pytest.raises(ValueError):
        compiler._compile_batch(inputs, keys[0], compiler._compile_budget(None), None, None,
                                journal_indexes=measure.journal_indexes, partitions=measure.partitions)


def test_final_partition_memo_rejects_changed_original_and_part_hash():
    parts = _parts()
    inputs = compiler.CompileInputs(tuple(parts), (), ())
    measure = compiler._ByteBatchMeasure(inputs)
    keys = {part.part_key for part in parts[:2]}
    measure(keys)
    changed = replace(parts[1], sha256="0" * 64)
    forged = replace(inputs, dailies=(parts[0], changed))
    with pytest.raises(ValueError):
        compiler._compile_batch(forged, keys, compiler._compile_budget(None), None, None,
                                partitions=measure.partitions)
    changed = replace(parts[1], original_content=parts[1].original_content + b"changed")
    forged = replace(inputs, dailies=(parts[0], changed))
    with pytest.raises(ValueError, match="different original bytes"):
        compiler._compile_batch(forged, keys, compiler._compile_budget(None), None, None,
                                partitions=measure.partitions)


def test_captured_partition_proof_is_reused_without_redecoding_original(tmp_path, monkeypatch):
    from tests.test_native_compile_uses_whole_container import _native_inputs
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "A" * 18000 + "\nActual fact.")
    calls = []
    original = compiler._partition_for_join

    def observed(part, **kwargs):
        calls.append(part.original_content)
        return original(part, **kwargs)

    monkeypatch.setattr(compiler, "_partition_for_join", observed)
    measure = compiler._ByteBatchMeasure(inputs)
    keys = {part.part_key for part in inputs.dailies}
    measure(keys)
    compiler._compile_batch(inputs, keys, compiler._compile_budget(None), None, None,
                            journal_indexes=measure.journal_indexes, partitions=measure.partitions)
    assert calls == []


@pytest.mark.parametrize("context_kind", ["stripped", "foreign_owner", "fake"])
def test_capture_context_requires_the_exact_input_owner(tmp_path, monkeypatch, context_kind):
    from tests.test_native_compile_uses_whole_container import _native_inputs
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "A" * 18000 + "\nActual fact.")
    contexts = {"stripped": None, "foreign_owner": inputs.partition_context, "fake": object()}
    changed = replace(inputs, partition_context=contexts[context_kind])
    measure = compiler._ByteBatchMeasure(changed)
    assert measure.partitions.sources == {}
    keys = {part.part_key for part in changed.dailies}
    measure(keys)
    assert measure.partitions.sources != {}
    assert measure.partitions is not inputs.partition_context.partitions


def test_empty_capture_context_cannot_claim_foreign_inputs():
    inputs = compiler.CompileInputs(tuple(_parts()), (), ())
    context = compiler._CapturedPartitions()
    with pytest.raises(ValueError, match="does not own"):
        context.bind(inputs)


def test_capture_partition_memo_still_rechecks_native_head(tmp_path, monkeypatch):
    from tests.test_native_compile_uses_whole_container import _native_inputs
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "A" * 18000 + "\nActual fact.")
    measure = compiler._ByteBatchMeasure(inputs)
    assert measure.partitions.sources != {}
    head = next(tmp_path.glob("knowledge/raw/sessions/**/*.breadcrumb.md"))
    head.write_bytes(head.read_bytes() + b"changed\n")
    keys = {part.part_key for part in inputs.dailies}
    with pytest.raises(ValueError):
        measure(keys)


def test_capture_partition_context_rejects_shadowed_input_tuple(tmp_path, monkeypatch):
    from tests.test_native_compile_uses_whole_container import _native_inputs
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "A" * 18000 + "\nActual fact.")
    object.__setattr__(inputs, "dailies", tuple(list(inputs.dailies)))
    measure = compiler._ByteBatchMeasure(inputs)
    assert measure.partitions.sources == {}


def test_captured_ranges_still_reject_missing_native_companion(tmp_path, monkeypatch):
    from tests.test_native_compile_uses_whole_container import _native_inputs
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "Полная строка. " * 3000)
    measure = compiler._ByteBatchMeasure(inputs)
    selected = [replace(part, native_frames=()) for part in inputs.dailies[:2]]
    with pytest.raises(ValueError, match="covering source parts"):
        compiler._deduplicated_sources(selected, partitions=measure.partitions,
                                       journal_indexes=measure.journal_indexes)


def test_atomic_admission_reuses_owned_capture_partition(tmp_path, monkeypatch):
    from tests.test_native_compile_uses_whole_container import _native_inputs
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "A" * 36000 + "\nActual fact.")
    calls = []
    original = compiler._partition_for_join

    def observed(part, **kwargs):
        calls.append(part.original_content)
        return original(part, **kwargs)

    monkeypatch.setattr(compiler, "_partition_for_join", observed)
    measure = compiler._ByteBatchMeasure(inputs)
    budget = compiler._compile_budget(None)
    unit = max(compiler._native_part_units(inputs.dailies), key=len)
    assert measure({part.part_key for part in unit}) > budget.available_input_tokens
    assert not compiler._unit_is_refused(unit, budget, measure)
    compiler._compile_batch(inputs, {part.part_key for part in unit}, budget, None, None,
                            journal_indexes=measure.journal_indexes, partitions=measure.partitions)
    assert calls == []


def test_joined_ordinary_parts_do_not_expand_the_native_atomic_budget():
    inputs = compiler.CompileInputs(tuple(_parts()), (), ())
    budget = compiler._compile_budget(None)
    batches = compiler.pack_compile_batches(inputs, model=None)
    assert len(batches) > 1
    assert {batch.packing.max_input_tokens for batch in batches} == {budget.max_input_tokens}
    assert all(batch.packing.measured_input_tokens <= budget.available_input_tokens for batch in batches)


def test_standalone_atomic_admission_recomputes_without_owned_context(tmp_path, monkeypatch):
    from tests.test_native_compile_uses_whole_container import _native_inputs
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "A" * 36000 + "\nActual fact.")
    unit = max(compiler._native_part_units(inputs.dailies), key=len)
    measure = compiler._ByteBatchMeasure(inputs)
    budget = compiler._compile_budget(None)
    count = measure({part.part_key for part in unit})
    admitted = compiler._atomic_unit_budget(unit, budget, count)
    assert admitted.available_input_tokens >= count
    changed = [replace(part, original_sha256="0" * 64) for part in unit]
    with pytest.raises(ValueError, match="canonical"):
        compiler._atomic_unit_budget(changed, budget, count)








def test_packing_does_not_rescan_all_parts_for_optional_context(monkeypatch):
    inputs = compiler.CompileInputs(tuple(_parts()), (), ())
    original = compiler._batch_text
    rescans = []

    def observed(*args, **kwargs):
        rescans.append(len(args[0].dailies))
        return original(*args, **kwargs)

    monkeypatch.setattr(compiler, "_batch_text", observed)
    batches = compiler.pack_compile_batches(inputs, model=None)
    assert len(batches) > 1
    assert rescans == []








def test_owned_part_lookup_preserves_order_and_repeated_objects():
    parts = _parts()
    inputs = compiler.CompileInputs((parts[0], parts[1], parts[0]), (), ())
    measure = compiler._ByteBatchMeasure(inputs)
    keys = {part.part_key for part in inputs.dailies}
    assert compiler._measure_batch_text(inputs, keys, measure) == compiler._batch_text(inputs, keys)
    changed = replace(inputs, dailies=tuple(reversed(inputs.dailies)))
    assert compiler._measure_batch_text(changed, keys, measure) == compiler._batch_text(changed, keys)
    assert compiler._measure_batch_text(inputs, keys, lambda paths: 0) == compiler._batch_text(inputs, keys)


def test_owned_part_lookup_refuses_changed_key_and_reads_current_bytes():
    parts = _parts()
    inputs = compiler.CompileInputs(tuple(parts), (), ())
    measure = compiler._ByteBatchMeasure(inputs)
    keys = {parts[0].part_key}
    object.__setattr__(parts[0], "content", b"changed current bytes")
    assert compiler._measure_batch_text(inputs, keys, measure) == compiler._batch_text(inputs, keys)
    object.__setattr__(parts[0], "logical_path", "knowledge/daily/changed.md")
    with pytest.raises(ValueError, match="identity changed"):
        compiler._measure_batch_text(inputs, keys, measure)
