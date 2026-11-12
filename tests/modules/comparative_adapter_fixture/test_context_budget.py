"""Retain the fixture, real subprocess tests, baseline payloads and history."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/comparative-adapter-fixture-context.json')
    print(f'comparative-adapter-fixture: {tokens} tokens; {files} files')
