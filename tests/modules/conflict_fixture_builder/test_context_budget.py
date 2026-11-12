"""Keep real optional qualification and complete source in the maintenance packet."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/conflict-fixture-builder-context.json')
    print(f'conflict-fixture-builder: {tokens} tokens; {files} files')
