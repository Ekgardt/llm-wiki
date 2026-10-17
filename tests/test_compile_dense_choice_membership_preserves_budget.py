"""Dense valid source lines must not spend the input budget on repeated IDs."""
import random

import compile_memory as cm
import pytest
from reliable_memory import SchemaValidationError, validate_schema_object

from tests.test_a_long_entry_is_cut_inside_itself import LOGICAL, _entry


def _dense_content():
    return _entry(1, b'\n## [12:26:31] pre-compact | session\n' +
                  b''.join(f'- {n}\n'.encode() for n in range(3000)))


def test_dense_short_lines_pack_without_changing_parts_source_or_budget():
    content = _dense_content()
    parts = tuple(cm._daily_parts(LOGICAL, content))
    inputs = cm.CompileInputs(parts, (), ())
    batches = cm.pack_compile_batches(inputs, model=None)
    actual_parts = [part for batch in batches for part in batch.inputs.dailies]
    assert actual_parts == list(parts)
    assert b''.join(part.content for part in actual_parts) == content
    assert all(batch.packing.measured_input_tokens <= batch.packing.max_input_tokens - batch.packing.reserved_output_tokens - batch.packing.safety_margin_tokens for batch in batches)
    assert cm._compile_budget(None).available_input_tokens == 27744


def _valid_identifier(identifier, schema):
    try:
        validate_schema_object(identifier, schema)
    except SchemaValidationError:
        return False
    return True


@pytest.mark.parametrize('ids', [list(range(144, 546)),
                                list(range(144, 546)) + list(range(700, 1100)),
                                [144, 145, 149, 196]])
def test_schema_membership_is_exact_for_dense_sparse_and_disjoint_ids(ids):
    schema = cm._source_id_schema([{'source_line': sid} for sid in ids])
    for value in range(-1, max(ids) + 2):
        assert _valid_identifier(value, schema) == (value in ids)
    assert len(cm.canonical_json_bytes(schema)) <= len(cm.canonical_json_bytes({'type': 'integer', 'enum': ids}))


def test_random_sparse_schema_membership_does_not_interpolate_gaps():
    randomizer = random.Random(611)
    for _case in range(12):
        ids = sorted(randomizer.sample(range(1, 1000), 80))
        schema = cm._source_id_schema([{'source_line': sid} for sid in ids])
        assert {value for value in range(1002) if _valid_identifier(value, schema)} == set(ids)


@pytest.mark.parametrize('value', [True, False, 144.0, 144.5, '144', None])
def test_local_integer_contract_refuses_non_integer_representations(value):
    schema = cm._source_id_schema([{'source_line': sid} for sid in range(144, 546)])
    with pytest.raises(SchemaValidationError):
        validate_schema_object(value, schema)


def test_small_membership_retains_the_existing_enum():
    assert cm._source_id_schema([{'source_line': 194}, {'source_line': 196}]) == {
        'type': 'integer', 'enum': [194, 196]}


def test_compressed_layout_is_counted_identically_by_byte_and_token_paths(monkeypatch):
    from tests.test_compile_plans_the_local_provider_layout import _byte_count

    monkeypatch.setenv('MEMORY_LLM_PROVIDER', 'fake')
    content = _entry(1, b'\n## [12:26:31] pre-compact | session\n' +
                     b''.join(f'- complete distinct fact {n}.\n'.encode() for n in range(80)))
    parts = tuple(cm._daily_parts(LOGICAL, content))
    inputs = cm.CompileInputs(parts, (), ())
    byte_measure = cm._ByteBatchMeasure(inputs)
    paths = {part.part_key for part in parts}
    subset = cm._subset_compile_inputs(inputs, paths, set(), partitions=byte_measure.partitions,
                                       journal_indexes=byte_measure.journal_indexes)
    expected = len(cm._draft_prompt_text(subset).encode())
    assert byte_measure(paths) == expected
    assert cm._TokenBatchMeasure(inputs, 'proof', {'proof': _byte_count})(paths) == expected
