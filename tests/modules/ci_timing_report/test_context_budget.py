"""The complete report compiler packet retains full dependency source."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/ci-timing-report-context.json')
    print(f'ci-timing-report: {tokens} tokens; {files} files')
