"""Measure complete decoder source, dependencies and exact history records."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/native-event-reader-context.json')
    print(f'native-event-reader: {tokens} tokens; {files} files')
