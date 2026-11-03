"""Complete saved-answer maintenance context, including historical contracts."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/compile-answer-records-context.json')
    print(f'compile-answer-records: {tokens} tokens; {files} files')
