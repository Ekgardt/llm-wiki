"""Whole policy, history, tests and instructions fit the maintenance ceiling."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/corpus-membership-policy-context.json')
    print(f'corpus-membership-policy: {tokens} tokens; {files} files')
