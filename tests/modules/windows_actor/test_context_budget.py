"""The complete native identity maintenance packet fits the user budget."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits_user_budget():
    total, files = measure_context(ROOT, ROOT / 'docs/modules/windows-actor-context.json')
    print(f'{total} tokens; {files} files')
