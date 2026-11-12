"""Retain protocol source, regression assertions, prompt bytes and complete history."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/classification-protocol-context.json')
    print(f'classification-protocol: {tokens} tokens; {files} files')
