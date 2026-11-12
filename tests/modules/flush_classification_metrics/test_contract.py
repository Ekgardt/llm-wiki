"""Full public fixture and edge cases of the original scoring contract."""
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'benchmark'))
import flush_classification_metrics as metrics  # noqa: E402

read_source = context_reader(ROOT, ROOT / 'docs/modules/flush-classification-metrics-context.json')


def test_entire_public_fixture_is_scored_without_mutation():
    corpus = json.loads(read_source(ROOT / 'benchmark/flush-classification-v1.json'))
    before = deepcopy(corpus)
    outcomes = [metrics.score_case(case, case['canned_response']) for case in corpus['cases']]
    measured = metrics.measure(outcomes)
    assert measured == {'case_count': 9, 'durable_case_count': 6, 'tier_accuracy': 1.0,
                        'durable_content_recall': 1.0, 'false_promotion_rate': 0.0}
    assert metrics.evaluate(measured, corpus['thresholds'])['passed'] is True
    assert corpus == before


def test_marker_recall_is_distinct_from_tier_accuracy():
    outcomes = [metrics.CaseOutcome('a', 'major', 'major', True, ('lost',), False),
                metrics.CaseOutcome('b', 'minor', 'major', False, (), False),
                metrics.CaseOutcome('c', 'ok', 'minor', False, (), True)]
    assert metrics.measure(outcomes) == {
        'case_count': 3, 'durable_case_count': 2, 'tier_accuracy': 0.333333,
        'durable_content_recall': 0.5, 'false_promotion_rate': 1.0,
    }


def test_empty_input_never_passes_despite_nominal_rates():
    measured = metrics.measure([])
    assert measured == {'case_count': 0, 'durable_case_count': 0, 'tier_accuracy': 1.0,
                        'durable_content_recall': 1.0, 'false_promotion_rate': 0.0}
    result = metrics.evaluate(measured, measured)
    assert result == {'metric_results': {'tier_accuracy': True, 'durable_content_recall': True,
                                        'false_promotion_rate': True, 'labelled': False}, 'passed': False}


def test_threshold_equality_passes_for_labelled_input():
    measured = {'case_count': 3, 'tier_accuracy': 0.5, 'durable_content_recall': 0.8,
                'false_promotion_rate': 0.2}
    assert metrics.evaluate(measured, measured)['passed'] is True


@pytest.mark.parametrize('field,value', [
    ('tier_accuracy', 0.49), ('durable_content_recall', 0.79), ('false_promotion_rate', 0.21),
])
def test_each_threshold_independently_rejects_a_miss(field, value):
    threshold = {'tier_accuracy': 0.5, 'durable_content_recall': 0.8, 'false_promotion_rate': 0.2}
    measured = {**threshold, 'case_count': 3, field: value}
    result = metrics.evaluate(measured, threshold)
    assert result['metric_results'][field] is False
    assert result['passed'] is False


def test_confirmed_filter_preserves_original_objects_and_default_v1_labels():
    cases = [{}, {'label_status': 'confirmed'}, {'label_status': 'contested'}, {'label_status': None}]
    corpus = {'cases': cases}
    before = deepcopy(corpus)
    confirmed = metrics.confirmed_cases(corpus)
    assert confirmed == cases[:2]
    assert confirmed[0] is cases[0]
    assert confirmed[1] is cases[1]
    assert metrics.label_state(corpus) == {'case_count': 4, 'confirmed_count': 2, 'contested_count': 2}
    assert corpus == before


def test_score_uses_case_sensitive_body_markers_and_string_identifiers():
    case = {'case_id': 7, 'expected_tier': 'major', 'required_markers': ['Rollback', 'rollback']}
    assert metrics.score_case(case, 'FLUSH_MAJOR\nRollback journal') == metrics.CaseOutcome(
        '7', 'major', 'major', True, ('rollback',), False,
    )


def test_score_preserves_string_coercion_of_expected_tier():
    case = {'case_id': None, 'expected_tier': 2, 'required_markers': []}
    assert metrics.score_case(case, 'FLUSH_OK') == metrics.CaseOutcome(
        'None', '2', 'ok', False, (), False,
    )
