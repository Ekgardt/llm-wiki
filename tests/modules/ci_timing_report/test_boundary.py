"""The CI report compiler imports its formats owner, never runtime executors."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/ci-timing-report-context.json')


def test_only_declared_dependencies():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/ci_timing_report.py')) == {
        'defusedxml.common', 'defusedxml.ElementTree', 'llm_wiki.formats.schema',
    }


def test_executor_imports_are_refused():
    assert_isolated_import(ROOT, 'ci_timing_report', dependencies=(
        'llm_wiki', 'llm_wiki.formats', 'llm_wiki.formats.schema',
    ))
