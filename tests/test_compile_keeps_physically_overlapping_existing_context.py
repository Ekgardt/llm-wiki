"""Known citation overlap is necessary context, independent of word ranking."""
import compile_memory as cm

from tests.test_compile_claims_producer import QUOTE, _daily, _evidence
from tests.test_compile_claims_producer import vault as vault


def _overlap_inputs(root):
    daily = _daily(root)
    initial = cm.snapshot_compile_inputs([daily])
    binding, _block = cm._bound_evidence_block(_evidence(), initial)
    note = root / 'knowledge/notes/project-rule.md'
    note.write_text('---\ntype: decision\n---\n# Accepted project boundary\n' + 'z' * 19000 + '\n\nEvidence: ' + binding['reference'] + '\n')
    other = root / 'knowledge/notes/lexical.md'
    other.write_text('---\ntype: pattern\n---\n' + (QUOTE + '\n') * 160)
    return cm.snapshot_compile_inputs([daily]), note.read_bytes()


def test_physically_overlapping_decision_survives_optional_word_ranking(vault):
    root, _state = vault
    inputs, original = _overlap_inputs(root)
    batches = cm.pack_compile_batches(inputs, model=None)
    assert len(batches) == 1
    sources = {item.logical_path: item.content for item in batches[0].inputs.sources}
    assert sources['knowledge/notes/project-rule.md'] == original
    assert batches[0].inputs.targets == inputs.targets


def _large_required(root):
    inputs, original = _overlap_inputs(root)
    note = root / 'knowledge/notes/project-rule.md'
    note.write_bytes(original.replace(b'z' * 19000, b'z' * 38000))
    return cm.snapshot_compile_inputs([root / inputs.dailies[0].logical_path])


def _basis(model, window):
    from types import SimpleNamespace

    basis = SimpleNamespace(model=model, planning_window=window)
    return (SimpleNamespace(provider='codex', model=model, _codex_basis=basis),)


def test_required_context_expands_only_to_measured_same_model_minimum(vault):
    root, _state = vault
    inputs = _large_required(root)
    model = 'captured-attempt-model'
    paths = {part.part_key for part in inputs.dailies}
    required = {'knowledge/notes/project-rule.md'}
    subset = cm._subset_compile_inputs(inputs, paths, required)
    count = cm._draft_prompt_count(subset, model, None).tokens
    target = cm._compile_budget(model)
    minimum = count + target.reserved_output_tokens + target.safety_margin_tokens
    batch = cm.pack_compile_batches(inputs, model=model, planning_candidates=_basis(model, minimum))[0]
    assert batch.packing.max_input_tokens == minimum
    assert batch.packing.measured_input_tokens == count
    assert {source.logical_path for source in batch.inputs.sources} == {inputs.dailies[0].logical_path, *required}


def test_unknown_window_does_not_drop_required_page_to_make_unit_fit(vault):
    import pytest

    root, _state = vault
    inputs = _large_required(root)
    with pytest.raises(ValueError, match='daily source exceeds compile input budget'):
        cm.pack_compile_batches(inputs, model=None)


def test_wrong_model_basis_cannot_expand_required_budget(vault):
    import pytest

    root, _state = vault
    inputs = _large_required(root)
    with pytest.raises(ValueError, match='daily source exceeds compile input budget'):
        cm.pack_compile_batches(inputs, model='actual-model', planning_candidates=_basis('other-model', 100000))


def _required(inputs):
    measure = cm._batch_measure(inputs, None, None)
    return cm._RequiredCitationContext(inputs, measure)


def test_changed_source_does_not_promote_old_citation_digest(vault):
    root, _state = vault
    inputs, _original = _overlap_inputs(root)
    daily = root / inputs.dailies[0].logical_path
    daily.write_bytes(daily.read_bytes() + b'\nChanged source.\n')
    current = cm.snapshot_compile_inputs([daily])
    required = _required(current)
    assert required.for_paths({part.part_key for part in current.dailies}) == set()
    assert required.unresolved > 0


def test_identical_bytes_at_a_different_logical_path_are_not_authority(vault):
    from dataclasses import replace

    root, _state = vault
    inputs, _original = _overlap_inputs(root)
    foreign = tuple(replace(part, logical_path=part.logical_path.replace('daily/', 'raw/')) for part in inputs.dailies)
    current = replace(inputs, dailies=foreign)
    assert _required(current).for_paths({part.part_key for part in foreign}) == set()


def test_required_context_snapshot_cannot_lie_about_its_content(vault):
    from dataclasses import replace

    import pytest

    root, _state = vault
    inputs, _original = _overlap_inputs(root)
    sources = tuple(replace(source, sha256='0' * 64) if source.logical_path.endswith('project-rule.md') else source
                    for source in inputs.sources)
    with pytest.raises(ValueError, match='snapshot hash'):
        _required(replace(inputs, sources=sources))


def test_small_same_model_capacity_refuses_complete_mandatory_input(vault):
    import pytest

    root, _state = vault
    inputs, _original = _overlap_inputs(root)
    with pytest.raises(ValueError, match='daily source exceeds compile input budget'):
        cm.pack_compile_batches(inputs, model='actual-model', planning_candidates=_basis('actual-model', 10000))


def test_invalid_policy_blocks_required_context_planning(vault, monkeypatch, tmp_path):
    import pytest

    root, _state = vault
    inputs, _original = _overlap_inputs(root)
    monkeypatch.setenv('LLM_WIKI_DLP_POLICY', str(tmp_path / 'missing-policy.json'))
    with pytest.raises(ValueError):
        cm.pack_compile_batches(inputs, model=None)


def _part_version_inputs(root):
    from dataclasses import replace

    from evidence_resolver import EvidenceRef

    daily = _daily(root)
    daily.write_bytes(daily.read_bytes() + b'Padding.\n' * 1100 + b'\n## [11:00:00] session-end | manual\nTail verified fact.\n' + b'Tail padding.\n' * 750)
    inputs = cm.snapshot_compile_inputs([daily])
    part = inputs.dailies[-1]
    start = part.content.index(b'Tail verified fact.')
    reference = EvidenceRef('2026-07-14', part.sha256, '11:00:00', start, start + len(b'Tail verified fact.'))
    note = root / 'knowledge/notes/part-version.md'
    note.write_text('---\ntype: decision\n---\nEvidence: ' + str(reference) + '\n')
    current = cm.snapshot_compile_inputs([daily])
    return replace(current, dailies=(current.dailies[-1],)), reference


def test_exact_part_version_bounds_are_projected_only_after_byte_proof(vault):
    root, _state = vault
    inputs, reference = _part_version_inputs(root)
    part = inputs.dailies[0]
    assert reference.source_sha256 == part.sha256 != part.original_sha256
    assert _required(inputs).for_paths({part.part_key}) == {'knowledge/notes/part-version.md'}


def test_part_local_offsets_cannot_be_reinterpreted_as_original_offsets(vault):
    from dataclasses import replace

    root, _state = vault
    inputs, reference = _part_version_inputs(root)
    part = inputs.dailies[0]
    wrong = replace(reference, source_sha256=part.original_sha256)
    note = root / 'knowledge/notes/part-version.md'
    note.write_text('---\ntype: decision\n---\nEvidence: ' + str(wrong) + '\n')
    current = cm.snapshot_compile_inputs([root / part.logical_path])
    selected = replace(current, dailies=(current.dailies[-1],))
    required = _required(selected)
    assert required.for_paths({selected.dailies[0].part_key}) == set()
    assert required.unresolved > 0


def _native_context(tmp_path, monkeypatch):
    from dataclasses import replace

    from tests.test_native_compile_uses_whole_container import _native_inputs, _native_operation

    inputs, _snapshot = _native_inputs(tmp_path, monkeypatch, 'A' * 18000 + '\nTail.')
    operation = _native_operation(inputs, 'Tail.')
    binding, _block = cm._bound_evidence_block(operation['evidence'][0], inputs)
    content = ('---\ntype: decision\n---\nEvidence: ' + binding['reference'] + '\n').encode()
    source = cm.SourceSnapshot('knowledge/notes/native-rule.md', content, cm.sha256_bytes(content))
    return replace(inputs, sources=(*inputs.sources, source)), content


def test_verified_native_container_keeps_full_existing_context(tmp_path, monkeypatch):
    inputs, original = _native_context(tmp_path, monkeypatch)
    paths = {part.part_key for part in inputs.dailies}
    required = {'knowledge/notes/native-rule.md'}
    subset = cm._subset_compile_inputs(inputs, paths, required)
    model = 'native-attempt-model'
    target = cm._compile_budget(model)
    count = cm._draft_prompt_count(subset, model, None).tokens
    window = count + target.reserved_output_tokens + target.safety_margin_tokens
    batch = cm.pack_compile_batches(inputs, model=model, planning_candidates=_basis(model, window))[0]
    assert len(batch.inputs.dailies) == len(inputs.dailies) >= 2
    assert next(source.content for source in batch.inputs.sources if source.logical_path in required) == original


def test_native_companion_cannot_be_omitted_for_required_context(tmp_path, monkeypatch):
    from dataclasses import replace

    import pytest

    inputs, _original = _native_context(tmp_path, monkeypatch)
    frame = inputs.dailies[0].native_frames[0]
    covering = [part for part in inputs.dailies if part.byte_start < frame.byte_end and part.byte_end > frame.byte_start]
    assert len(covering) >= 2
    incomplete = replace(inputs, dailies=(covering[0],))
    with pytest.raises(ValueError, match='cover|complete|part'):
        cm.pack_compile_batches(incomplete, model=None)


def test_native_frame_metadata_change_is_not_a_context_authority_cache_hit(tmp_path, monkeypatch):
    from dataclasses import replace

    import pytest

    inputs, _original = _native_context(tmp_path, monkeypatch)
    frame = replace(inputs.dailies[0].native_frames[0], encoded='{}')
    parts = tuple(replace(part, native_frames=(frame,)) for part in inputs.dailies)
    with pytest.raises(ValueError, match='canonical|container'):
        cm.pack_compile_batches(replace(inputs, dailies=parts), model=None)


def test_previously_required_page_cannot_disappear_during_refresh(vault):
    import pytest

    root, _state = vault
    inputs, _original = _overlap_inputs(root)
    batch = cm.pack_compile_batches(inputs, model=None)[0]
    (root / 'knowledge/notes/project-rule.md').unlink()
    with pytest.raises(ValueError, match='required.*context|context.*lost'):
        cm._refresh_compile_batch(batch)


def test_different_claims_sharing_known_decision_evidence_are_not_dropped(vault):
    from tests.test_compile_claims_producer import _candidate, _operation

    root, _state = vault
    inputs, _original = _overlap_inputs(root)
    selected = cm.pack_compile_batches(inputs, model=None)[0].inputs
    operation = _operation([_candidate(), _candidate(subject='project maintenance lease')])
    operations = cm._with_derived_claims([operation], selected)
    semantic, _bindings = cm._validate_semantic_operation(operations[0], selected)
    assert [record['subject'] for record in semantic['claims']] == ['maintenance lease', 'project maintenance lease']
    assert 'SELECTED EXISTING CONTEXT' in cm._critique_prompt(selected, operations)


def test_pending_required_context_refreshes_once_without_budget_or_manifest_drift(vault, monkeypatch):
    root, _state = vault
    inputs = _large_required(root)
    model = 'captured-attempt-model'
    target = cm._compile_budget(model)
    paths = {part.part_key for part in inputs.dailies}
    subset = cm._subset_compile_inputs(inputs, paths, {'knowledge/notes/project-rule.md'})
    count = cm._draft_prompt_count(subset, model, None).tokens
    candidates = _basis(model, count + target.reserved_output_tokens + target.safety_margin_tokens)
    batch = cm._pack_compile_batches(inputs, model=model, planning_candidates=candidates, context_pending=True)[0]
    monkeypatch.setattr(cm, '_refreshed_batch_candidates', lambda previous, deadline: candidates)
    ready = cm._refresh_compile_batch(batch)
    assert batch.context_pending and not ready.context_pending
    assert ready.required_context_paths == batch.required_context_paths
    assert ready.manifest == batch.manifest
    assert ready.packing.max_input_tokens == batch.packing.max_input_tokens


def test_changed_required_target_refuses_normal_publication(vault):
    import pytest
    from markdown_transaction import MarkdownCoordinator, TransactionFailure

    from tests.test_compile_claims_producer import _operation

    root, state = vault
    inputs, _original = _overlap_inputs(root)
    selected = cm.pack_compile_batches(inputs, model=None)[0].inputs
    plan = cm._normalize_plan([_operation()], selected)
    note = root / 'knowledge/notes/project-rule.md'
    note.write_bytes(note.read_bytes() + b'\nExternal change.\n')
    with pytest.raises(TransactionFailure, match='persisted precondition failed') as refusal:
        cm.apply_compile_plan(selected, plan, action_key='a' * 64, trigger='manual',
                              coordinator=MarkdownCoordinator(root, state))
    assert refusal.value.code == 'precondition_failed'
    assert not (root / 'knowledge/notes/bounded-lease-expiry.md').exists()


def _repeated_part_inputs(root):
    from evidence_resolver import EvidenceRef

    daily = _daily(root)
    prefix = b'\n## [11:00:00] session-end | manual\nRepeated verified fact.\n'
    block = prefix + b'x' * (cm.MAX_DAILY_PART_BYTES - len(prefix) - 1) + b'\n'
    daily.write_bytes(block * 2)
    inputs = cm.snapshot_compile_inputs([daily])
    first, second = inputs.dailies
    assert first.content == second.content == block
    start = block.index(b'Repeated verified fact.')
    reference = EvidenceRef('2026-07-14', first.sha256, '11:00:00', start, start + len(b'Repeated verified fact.'))
    note = root / 'knowledge/notes/repeated-part.md'
    note.write_text('---\ntype: decision\n---\nEvidence: ' + str(reference) + '\n')
    return cm.snapshot_compile_inputs([daily]), reference


def test_part_digest_without_unique_physical_origin_cannot_require_context(vault):
    root, _state = vault
    inputs, _reference = _repeated_part_inputs(root)
    required = _required(inputs)
    assert required.for_paths({part.part_key for part in inputs.dailies}) == set()
    assert required.unresolved > 0


def test_original_absolute_span_keeps_its_exact_repeated_part_origin(vault):
    from dataclasses import replace

    root, _state = vault
    inputs, reference = _repeated_part_inputs(root)
    first, second = inputs.dailies
    absolute = replace(reference, source_sha256=first.original_sha256)
    note = root / 'knowledge/notes/repeated-part.md'
    note.write_text('---\ntype: decision\n---\nEvidence: ' + str(absolute) + '\n')
    current = cm.snapshot_compile_inputs([root / first.logical_path])
    required = _required(current)
    assert required.for_paths({first.part_key}) == {'knowledge/notes/repeated-part.md'}
    assert required.for_paths({second.part_key}) == set()
