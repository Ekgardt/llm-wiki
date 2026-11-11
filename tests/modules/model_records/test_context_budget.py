"""The entire model value maintenance packet fits the owner's ceiling."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_model_records_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/model-records-context.json')
    print(f'model-records: {tokens} tokens; {files} files')
