"""Count the complete declared bundle, including pytest bootstrap and framing."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_sqlite_contracts_context_fits_user_budget():
    total, files = measure_context(ROOT, ROOT / 'docs/modules/sqlite-contracts-context.json')
    print(f'docs/modules/sqlite-contracts-context.json: {total} tokens; {files} files')
