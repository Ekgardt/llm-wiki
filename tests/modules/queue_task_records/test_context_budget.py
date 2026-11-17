"""Measure full task representation, schema, captured outputs and dependencies."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_record_context_fits_user_budget():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/queue-task-records-context.json')
    print(f'queue-task-records: {tokens} tokens; {files} files')
