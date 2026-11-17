"""Reader leases retain complete source, tests and historical context."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/evidence-reader-cache-context.json')
    print(f'evidence-reader-cache: {tokens} tokens; {files} files')
