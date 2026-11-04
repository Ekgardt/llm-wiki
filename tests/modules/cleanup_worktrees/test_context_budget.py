"""Measure complete decoder source, dependencies and exact history records."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/cleanup-worktrees-context.json')
    print(f'cleanup-worktrees: {tokens} tokens; {files} files')
