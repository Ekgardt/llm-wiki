"""The full lifecycle packet must fit the owner's context limit."""
from pathlib import Path

from tests.module_context import measure_context

ROOT = Path(__file__).resolve().parents[3]


def test_complete_context_fits():
    measure_context(ROOT, ROOT / 'docs/modules/compile-call-runtime-context.json')
