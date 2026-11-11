"""Complete release-pending context obeys the unchanged maintenance ceiling."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/release-pending-context.json')
    print(f'release-pending: {tokens} tokens; {files} files')
