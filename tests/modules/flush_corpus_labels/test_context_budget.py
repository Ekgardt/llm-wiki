"""Retain the pure labels, original assertions, schema and complete history."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/flush-corpus-labels-context.json')
    print(f'flush-corpus-labels: {tokens} tokens; {files} files')
