"""The full owner maintenance packet fits the user's token ceiling."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_packet_fits_user_budget():
    total, files = measure_context(ROOT, ROOT / 'docs/modules/embedding-model-context.json')
    print(f'{total} tokens; {files} files')
