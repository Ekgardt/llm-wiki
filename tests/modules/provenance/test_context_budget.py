"""The full provenance owner retains source, tests, fixtures and policy history."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/provenance-context.json')
    print(f'provenance: {tokens} tokens; {files} files')
