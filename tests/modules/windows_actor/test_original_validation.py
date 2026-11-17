"""Recorded outcomes come from the original probe, including refusal details."""
import json
from pathlib import Path

import pytest
from llm_wiki import windows_actor

CASES = json.loads((Path(__file__).with_name('original-validation.json')).read_text(encoding='utf-8'))


def outcome(value):
    try:
        return {'value': windows_actor.validated_windows_actor(value)}
    except Exception as error:
        return {'error': type(error).__name__, 'message': str(error)}


@pytest.mark.parametrize('case', CASES)
def test_original_validation_outcome(case):
    assert outcome(case['input']) == case['outcome']
