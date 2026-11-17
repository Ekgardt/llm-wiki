"""The complete existing owner must fit the user's maintenance budget."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/temporal-anchor-context.json')
    print(f'temporal-anchor: {tokens} tokens; {files} files')
