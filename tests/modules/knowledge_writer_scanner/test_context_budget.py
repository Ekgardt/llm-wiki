"""Writer analysis retains source, tests and historical context."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/knowledge-writer-scanner-context.json')
    print(f'knowledge-writer-scanner: {tokens} tokens; {files} files')
