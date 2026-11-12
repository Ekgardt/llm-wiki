"""Complete original report source, tests and history remain measurable."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/selective-forgetting-metrics-context.json')
    print(f'selective-forgetting-metrics: {tokens} tokens; {files} files')
