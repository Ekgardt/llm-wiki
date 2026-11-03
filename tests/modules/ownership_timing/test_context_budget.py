"""Complete source, tests and instructions retain the user's token limit."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits_user_limit():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/ownership-timing-context.json')
    print(f'ownership-timing: {tokens} tokens; {files} files')
