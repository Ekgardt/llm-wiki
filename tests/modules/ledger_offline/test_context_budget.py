"""Offline counting retains real dependencies, tests and historical context."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/ledger-offline-context.json')
    print(f'ledger-offline: {tokens} tokens; {files} files')
