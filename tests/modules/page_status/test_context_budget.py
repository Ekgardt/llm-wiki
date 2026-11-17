"""The full owned maintenance bundle fits the user's limit."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits_user_limit():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/page-status-context.json')
    print(f'page-status: {tokens} tokens; {files} files')
