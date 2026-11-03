"""Artifact IO has one owner and never imports queue execution or SQL."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports

ROOT = Path(__file__).resolve().parents[3]






def test_only_explicit_lower_dependencies_including_lazy_imports():
    source = (ROOT / 'scripts/llm_wiki/queue_artifacts.py').read_text()
    assert nonstdlib_imports(source) <= {
        'llm_wiki.queue_contracts', 'llm_wiki.runtime_environment',
        'llm_wiki.runtime_sync', 'llm_wiki.sqlite_deadlines', 'llm_wiki.windows_acl',
    }


def test_import_does_not_load_execution_or_compile_journal():
    assert_isolated_import(ROOT, 'llm_wiki.queue_artifacts', dependencies=(
        'llm_wiki', 'llm_wiki.queue_contracts', 'llm_wiki.runtime_environment',
        'llm_wiki.runtime_sync', 'llm_wiki.runtime_contracts',
        'llm_wiki.sqlite_deadlines', 'llm_wiki.windows_acl', 'llm_wiki.windows_native',
        'windows_workspace',
    ))
