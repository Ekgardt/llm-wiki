"""Measure full value-owner source, tests, history and dependencies."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/event-envelope-values-context.json')
    print(f'event-envelope-values: {tokens} tokens; {files} files')
