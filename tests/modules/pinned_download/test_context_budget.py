"""Measure the complete maintenance packet."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_packet_fits_budget():
    total, files = measure_context(ROOT, ROOT / 'docs/modules/pinned-download-context.json')
    print(f'{total} tokens; {files} files')
