"""Complete deterministic generator context remains bounded and exact."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/python-qualification-corpus-context.json')
    print(f'python-qualification-corpus: {tokens} tokens; {files} files')
