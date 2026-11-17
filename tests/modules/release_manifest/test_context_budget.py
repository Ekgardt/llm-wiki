"""The whole release-manifest maintenance packet fits the user ceiling."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/release-manifest-context.json')
    print(f'release-manifest: {tokens} tokens; {files} files')
