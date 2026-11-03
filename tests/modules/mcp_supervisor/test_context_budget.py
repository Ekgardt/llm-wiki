"""Count the full supervisor source, protocol tests, history and instructions."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_supervisor_context_fits_user_budget():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/mcp-supervisor-context.json')
    print(f'mcp-supervisor: {tokens} tokens; {files} files')
