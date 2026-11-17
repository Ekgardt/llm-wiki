"""Complete original report source, tests and history remain measurable."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/benchmark-arm-comparison-context.json')
    print(f'benchmark-arm-comparison: {tokens} tokens; {files} files')
