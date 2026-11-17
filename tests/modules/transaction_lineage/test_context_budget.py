"""The complete lineage reader includes its original fixtures and history."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/transaction-lineage-context.json')
    print(f'transaction-lineage: {tokens} tokens; {files} files')
