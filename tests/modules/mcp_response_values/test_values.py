"""Response validation and normalization preserve existing wire values."""
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from llm_wiki import mcp_response_values as values


@pytest.mark.parametrize('value', [True, False, -0.01, 1.01, float('nan'), float('inf'), '1'])
def test_quality_rejects_non_numeric_or_out_of_range_values(value):
    with pytest.raises(ValueError, match='coverage must be a number between 0 and 1'):
        values.bounded('coverage', value)


@pytest.mark.parametrize('value', [0, 1, 0.25])
def test_quality_keeps_inclusive_endpoints_as_floats(value):
    actual = values.bounded('confidence', value)
    assert actual == value
    assert type(actual) is float


def test_supplied_times_normalize_without_reading_a_clock():
    plain = datetime(2026, 7, 13, 12, 30)
    offset = datetime(2026, 7, 13, 15, 30, tzinfo=timezone(timedelta(hours=3)))
    expected = datetime(2026, 7, 13, 12, 30, tzinfo=UTC)
    assert values.as_utc(plain) == expected
    assert values.as_utc(offset) == expected


@pytest.mark.parametrize(('states', 'expected'), [
    ([], 'unknown'), (['fresh'], 'fresh'), (['fresh', 'stale'], 'stale'),
    (['fresh', 'missing'], 'unknown'), (['unknown'], 'unknown'),
])
def test_freshness_retains_uncertainty_and_staleness(states, expected):
    supplied = {str(i): {'generation': None, 'freshness': state} for i, state in enumerate(states)}
    assert values.freshness(values.components(supplied)) == expected


@pytest.mark.parametrize('detail', [
    {'generation': '', 'freshness': 'fresh'},
    {'generation': None, 'freshness': 'invented'},
    {'generation': None, 'freshness': 'fresh', 'extra': True},
])
def test_component_contract_rejects_invalid_fields(detail):
    with pytest.raises(ValueError):
        values.components({'graph': detail})


class TextOnly:
    def __str__(self):
        return 'non-JSON value'


def test_nested_values_become_strict_json_without_losing_paths_or_scalar_types():
    supplied = {7: (Path('page.md'), float('nan'), True), 'tail': [float('inf'), TextOnly()]}
    actual = values.json_safe(supplied)
    assert actual == {'7': ['page.md', None, True], 'tail': [None, 'non-JSON value']}
    assert json.loads(json.dumps(actual, allow_nan=False)) == actual


def test_schema_mutation_does_not_change_later_tool_declarations():
    first = values.envelope_schema()
    first['properties']['coverage']['maximum'] = 99
    first['required'].clear()
    second = values.envelope_schema()
    assert second['properties']['coverage']['maximum'] == 1
    assert 'coverage' in second['required']
