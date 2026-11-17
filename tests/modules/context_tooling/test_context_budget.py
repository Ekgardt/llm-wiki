"""Count the complete declared bundle, including pytest bootstrap and framing."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_tooling_context_fits_user_budget():
    total, files = measure_context(ROOT, ROOT / 'docs/modules/context-tooling-context.json')
    print(f'docs/modules/context-tooling-context.json: {total} tokens; {files} files')
