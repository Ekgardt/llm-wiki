"""The reader packet includes complete source, policy dependencies and evidence."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/page-facts-context.json')
    print(f'page-facts: {tokens} tokens; {files} files')
