"""Time values must not load registry, process probes or filesystem execution."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/ownership-timing-context.json')


def test_time_policy_has_only_value_dependencies():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/llm_wiki/ownership_timing.py')) == {
        'iso_time', 'llm_wiki.ownership_contracts',
    }
    assert_isolated_import(ROOT, 'llm_wiki.ownership_timing', dependencies=(
        'llm_wiki', 'iso_time', 'llm_wiki.ownership_contracts',
        'llm_wiki.runtime_contracts',
    ))
