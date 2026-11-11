"""Policies retain full implementations, original tests and historical context."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/answer-refinement-context.json')
    print(f'answer-refinement: {tokens} tokens; {files} files')
