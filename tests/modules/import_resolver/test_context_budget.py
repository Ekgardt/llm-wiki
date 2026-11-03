"""The resolver retains full source, registry tests, probe and history."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/import-resolver-context.json')
    print(f'import-resolver: {tokens} tokens; {files} files')
