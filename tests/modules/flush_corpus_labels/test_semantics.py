"""Parsing, provenance and agreement retain their established edge cases."""
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tests.modules.flush_corpus_labels.contract_helpers import _builder

labels = _builder()


def test_rubric_filters_known_kinds_and_verbatim_short_markers():
    text = json.dumps({'keeps': ' MUCH ', 'kinds': ['Lesson', 'decision', 'unknown', 'lesson'],
                       'phrases': ['rollback', 'not present', 'one two three four five six', 'journal']})
    answer = labels.parse_verdict('prefix ' + text + ' trailing {comment}', 'rollback journal')
    assert answer == labels.Verdict('major', ('decision', 'lesson'), ('rollback', 'journal'))


@pytest.mark.parametrize('answer', ['plain text', '{', '{"keeps": "unknown"}'])
def test_unreadable_or_unknown_verdict_is_refused(answer):
    with pytest.raises(ValueError):
        labels.parse_verdict(answer, 'source')


def test_only_three_surviving_markers_are_retained():
    answer = labels.parse_verdict(
        json.dumps({'keeps': 'little', 'phrases': ['one', 'two', 'three', 'four']}),
        'one two three four',
    )
    assert answer.phrases == ('one', 'two', 'three')
    assert answer.kinds == ()


def test_frozen_readings_keep_their_field_contract():
    reading = labels.QuoteReading('ok', '')
    with pytest.raises(FrozenInstanceError):
        reading.tier = 'major'
    assert repr(reading) == "QuoteReading(tier='ok', passage='')"


def test_case_preserves_provenance_language_and_contested_reading():
    verdict = labels.Verdict('ok', (), ('ignored marker',))
    case = labels.build_case(Path('Тест.jsonl'), 3, 'Привет мир', verdict, labels.QuoteReading(None, 'void'))
    assert case == {
        'case_id': 'live-003', 'language': 'RU', 'event': 'session-end',
        'content_classes': [], 'transcript': 'Привет мир', 'expected_tier': 'ok',
        'required_markers': [], 'label_provenance': 'readings', 'label_status': 'contested',
        'readings': {'rubric': 'ok', 'quote': None, 'passage': 'void'},
    }


@pytest.mark.parametrize('pairs, expected', [
    ([], None),
    ([('major', 'major')] * 30, None),
    ([('major', 'major'), ('minor', 'minor'), ('ok', 'ok')] * 10, 1.0),
    ([('major', 'minor'), ('minor', 'ok'), ('ok', 'major')] * 10, -0.5),
])
def test_degenerate_perfect_and_disagreeing_kappa(pairs, expected):
    assert labels.cohens_kappa(pairs) == expected


def test_agreement_excludes_void_pairs_and_formats_unavailable_value():
    cases = [{'label_status': 'contested', 'readings': {'rubric': 'major', 'quote': None}}] * 30
    counts = labels.agreement(cases)
    assert counts == {'confirmed': 0, 'contested': 30, 'kappa': None}
    assert labels.agreement_line({'agreement': counts}) == 'labels: 0 confirmed, 30 contested; readings\' kappa not reported'
    assert labels.agreement_line({'agreement': {**counts, 'kappa': 0.5}}).endswith('kappa 0.500')
