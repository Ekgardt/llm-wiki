"""Daily selection retains source, tests, history and instructions."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/daily-log-selection-context.json')
    print(f'daily-log-selection: {tokens} tokens; {files} files')
