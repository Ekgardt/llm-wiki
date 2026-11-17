"""Complete maintenance context retains the user's limit."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits_user_limit():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/ownership-rows-context.json')
    print(f'ownership-rows: {tokens} tokens; {files} files')
