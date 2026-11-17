"""Retain full protocol, schemas, dependency sources and original assertions."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/breadcrumb-documents-context.json')
    print(f'breadcrumb-documents: {tokens} tokens; {files} files')
