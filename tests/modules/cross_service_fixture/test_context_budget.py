"""Retain the builder, real Git tests, task data and historical rationale."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/cross-service-fixture-context.json')
    print(f'cross-service-fixture: {tokens} tokens; {files} files')
