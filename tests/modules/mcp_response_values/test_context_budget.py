"""Response values retain source, tests, schema history and instructions."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/mcp-response-values-context.json')
    print(f'mcp-response-values: {tokens} tokens; {files} files')
