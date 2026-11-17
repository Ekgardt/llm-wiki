"""Full storage source, tests, contracts and exact history stay within the ceiling."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_storage_context_fits_user_budget():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/compile-progress-context.json')
    print(f'compile-progress: {tokens} tokens; {files} whole files plus exact history records')
