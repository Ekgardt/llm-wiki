"""State values retain source, tests, budget history and instructions."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/ledger-core-context.json')
    print(f'ledger-core: {tokens} tokens; {files} files')
