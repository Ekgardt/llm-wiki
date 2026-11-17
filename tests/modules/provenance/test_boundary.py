"""Weighted policy reads need only the pure shared membership owner."""
import subprocess
import sys
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, nonstdlib_imports
from tests.module_context import context_reader
from tests.slow_machine import LONG_TIMEOUT

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/provenance-context.json')


def test_provenance_has_one_pure_local_dependency():
    assert nonstdlib_imports(read_source(ROOT / 'scripts/provenance.py')) == {'llm_wiki.corpus_membership_policy'}


def test_provenance_import_is_independent_of_runtime_owners():
    assert_isolated_import(ROOT, 'provenance', dependencies=('llm_wiki', 'llm_wiki.corpus_membership_policy'))


def test_an_actual_weighted_read_does_not_load_the_collector():
    program = '''import sys
sys.path.insert(0, sys.argv[1])
import provenance
assert provenance.source_type_weight('decision', 'docs/spec.md', curated_first=True) == provenance.TYPE_WEIGHTS['doc']
assert 'corpus_snapshot' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', program, str(ROOT / 'scripts')],
                            capture_output=True, text=True, timeout=LONG_TIMEOUT)
    assert result.returncode == 0, result.stdout + result.stderr
