"""Editorial policy retains full source, publication evidence and instructions."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/vault-editorial-context.json')
    print(f'vault-editorial: {tokens} tokens; {files} files')
