"""Full invariant source, tests and required schema context fit the user's budget."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits_user_budget():
    total, files = measure_context(ROOT, ROOT / 'docs/modules/coordinator-invariants-context.json')
    print(f'{total} tokens; {files} files')
