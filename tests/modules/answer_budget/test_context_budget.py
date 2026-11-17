"""Full source, contract, tests and history fit the maintenance ceiling."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    tokens, files = measure_context(ROOT, ROOT / 'docs/modules/answer-budget-context.json')
    print(f'answer-budget: {tokens} tokens; {files} files')
