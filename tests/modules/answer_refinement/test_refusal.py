"""Refusal policy prepares bounded missing-evidence queries for its caller."""
import json

import pytest
import refusal_pass


@pytest.mark.parametrize(('answer', 'expected'), [
    ({'status': 'answered'}, False),
    ({'status': 'insufficient_evidence'}, True),
    ({'status': 'answered', 'dropped_claims': [{'text': '  missing detail  '}]}, True),
    ({'status': 'answered', 'dropped_claims': [{'text': ' '}]}, False),
])
def test_refusal_or_nonempty_dropped_claim_requests_another_search(answer, expected):
    assert refusal_pass.needs_a_second_search(answer) is expected


def test_query_preparation_keeps_the_reason_and_dropped_claims_as_data():
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return json.dumps({'queries': ['Where?', '', 'found', 'found', 3, 'another']})

    assert refusal_pass.missing_queries(' Where? ', ' missing store ', ['A claim'], ask) == ['found', 'another']
    assert prompts == ['<question>\nWhere?\n</question>\n<reason>\nmissing store\n</reason>\n<unsupported>\n- A claim\n</unsupported>']


def test_existing_refusal_query_limit_and_invalid_reply_behavior():
    reply = json.dumps({'queries': [f'query {i}' for i in range(9)]})
    assert refusal_pass.missing_queries('question', '', [], lambda _p: reply) == [f'query {i}' for i in range(5)]
    assert refusal_pass.missing_queries('question', '', [], lambda _p: None) == []
