"""Whole pre-refactor receipt bytes remain identical, including large inputs."""
import json
from pathlib import Path

import breadcrumb_decision as decision
import pytest

from tests.breadcrumb_decision_helpers import _bundle
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/breadcrumb-decision-context.json')
BASELINE = json.loads(read_source(Path(__file__).with_name('receipt_baseline.json')))


@pytest.mark.parametrize('case', BASELINE['samples'])
def test_original_receipt_bytes(case):
    bundle = _bundle(case['text'] * case['repeat'])
    expected = case['receipt'].encode()
    assert decision.decision_bytes(bundle) == expected
    assert decision.read_decision(expected, bundle) == json.loads(case['receipt'])
