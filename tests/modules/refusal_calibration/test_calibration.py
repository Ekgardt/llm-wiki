"""Offline gates report actual decisions, including an unavailable threshold."""
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from benchmark import calibrate_refusal_gates as calibration
from tests.slow_machine import LONG_TIMEOUT

ROOT = Path(__file__).resolve().parents[3]


def _reading(identifier, original, twin):
    return calibration.Reading(
        {'question_id': identifier, 'status': 'abstained'},
        calibration.Measure(original, None, False),
        calibration.Measure(twin, None, False),
    )


@pytest.mark.parametrize('threshold,hit,false_alarm', [(None, 0.0, 0.0), (0.0, 1.0, 1.0), (0.75, 0.5, 0.0)])
def test_report_distinguishes_unavailable_zero_and_positive_threshold(threshold, hit, false_alarm):
    items = [_reading('q0', 0.9, 0.3), _reading('q1', 0.4, 0.6)]
    report = calibration._half_report(items, threshold)
    assert (report['coverage_of_originals'], report['false_fire_on_twins']) == (hit, false_alarm)


def test_no_admissible_threshold_never_reports_firing():
    report = calibration.calibrate([_reading('q0', 0.5, 0.5), _reading('q1', 0.5, 0.5)], 0.0)
    assert report['sufficient_to_read_again_from_tune'] is None
    assert report['refusals_read_again'] == 0
    assert report['tune']['coverage_of_originals'] == 0.0
    assert report['tune']['false_fire_on_twins'] == 0.0
    assert report['decide']['coverage_of_originals'] == 0.0
    assert report['decide']['false_fire_on_twins'] == 0.0


def test_decide_scores_cannot_change_the_tuned_threshold():
    tune = _reading('q0', 0.8, 0.2)
    first = calibration.calibrate([tune, _reading('q1', 0.9, 0.3)], 0.0)
    second = calibration.calibrate([tune, _reading('q1', 0.0, 1.0)], 0.0)
    assert first['sufficient_to_read_again_from_tune'] == second['sufficient_to_read_again_from_tune'] == 0.8
    assert first['tune'] == second['tune']
    assert first['decide'] != second['decide']


@pytest.mark.parametrize('positive,negative,expected', [([1, 2], [0, 1], 0.875), ([1], [1], 0.5), ([], [1], None), ([1], [], None)])
def test_auc_counts_ties_and_missing_populations(positive, negative, expected):
    assert calibration.auc(positive, negative) == expected


def test_sensitivity_is_finite_at_extremes_and_zero_for_equal_rates():
    assert calibration.dprime_at(0.5, 0.5) == 0.0
    assert calibration.dprime_from_auc(0.5) == 0.0
    assert math.isfinite(calibration.dprime_at(1.0, 0.0))


def _question(identifier):
    return {
        'question_id': identifier, 'question': 'Where do I keep my blue bicycle?',
        'question_date': '2026-01-02', 'answer_session_ids': ['evidence'],
        'haystack_session_ids': ['evidence', 'distractor'],
        'haystack_dates': ['2026-01-01', '2026-01-01'],
        'haystack_sessions': [
            [{'role': 'user', 'content': 'I keep my blue bicycle in the garage.', 'has_answer': True}],
            [{'role': 'user', 'content': 'I like tea.'}],
        ],
    }


def test_twin_removes_evidence_before_real_measurement():
    item = calibration._readings_of({'question_id': 'q0'}, _question('q0'), 12)
    assert item.original.score > item.twin.score
    assert item.twin.score == 0.0


def _staged_rows(root):
    rows = []
    for identifier in ('q0', 'q1'):
        (root / f'{identifier}.question.json').write_text(json.dumps(_question(identifier)), encoding='utf-8')
        rows.append({'question_id': identifier, 'status': 'abstained'})
    path = root / 'results.jsonl'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    return path


def test_cli_reads_real_staged_files_and_writes_only_requested_report(tmp_path):
    rows = _staged_rows(tmp_path)
    output = tmp_path / 'report.json'
    result = subprocess.run(
        [sys.executable, str(ROOT / 'benchmark/calibrate_refusal_gates.py'),
         '--results', str(rows), '--staging', str(tmp_path), '--alpha', '0', '--json', str(output)],
        capture_output=True, text=True, timeout=LONG_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['tune']['n'] == report['decide']['n'] == 1
    assert report['tune']['coverage_of_originals'] == report['decide']['coverage_of_originals'] == 1.0
    assert report['tune']['false_fire_on_twins'] == report['decide']['false_fire_on_twins'] == 0.0
    assert report['refusals_read_again'] == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ['q0.question.json', 'q1.question.json', 'report.json', 'results.jsonl']
