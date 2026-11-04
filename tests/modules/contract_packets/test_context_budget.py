"""Packet composition retains its full source, tests and paired history."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits_user_limit():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/contract-packets-context.json')
    print(f'contract-packets: {tokens} tokens; {files} files')
