"""The builder retains full source, tests, profile values and history."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/go-source-build-context.json')
    print(f'go-source-build: {tokens} tokens; {files} files')
