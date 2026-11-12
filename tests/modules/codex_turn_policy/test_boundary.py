"""Turn policy cannot import the state writer or capture execution owners."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/codex-turn-policy-context.json')


def test_policy_has_no_product_dependencies():
    assert not nonstdlib_imports(read_source(ROOT / 'scripts/codex_turn_end.py'))
    assert_isolated_import(ROOT, 'codex_turn_end')
