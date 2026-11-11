"""The maintenance codec is independent of runtime and filesystem operations."""
import ast
import subprocess
import sys
from pathlib import Path

from tests.module_boundaries import assert_stdlib_boundary
from tests.module_context import context_reader
from tests.slow_machine import LONG_TIMEOUT

ROOT = Path(__file__).resolve().parents[3]


read_source = context_reader(ROOT, ROOT / 'docs/modules/context-bundle-context.json')


def test_source_uses_only_standard_library():
    assert_stdlib_boundary(ROOT, 'llm_wiki/context_bundle', read_source=read_source)


def test_codec_does_not_read_or_write_files():
    tree = ast.parse(read_source(ROOT / 'scripts/llm_wiki/context_bundle.py'))
    calls = [n.func for n in ast.walk(tree) if isinstance(n, ast.Call)]
    names = {getattr(n, 'attr', getattr(n, 'id', '')) for n in calls}
    assert not names.intersection({'open', 'read_bytes', 'write_bytes', 'read_text', 'write_text'})


def test_import_does_not_load_any_other_product_owner():
    program = '''
import importlib.abc, sys
sys.path.insert(0, sys.argv[1])
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        assert not fullname.startswith('llm_wiki.') or fullname == 'llm_wiki.context_bundle', fullname
sys.meta_path.insert(0, Guard())
from llm_wiki.context_bundle import pack_files, unpack_files
assert unpack_files(pack_files({'a': b'x'})) == {'a': b'x'}
'''
    result = subprocess.run([sys.executable, '-c', program, str(ROOT / 'scripts')],
                            capture_output=True, text=True, timeout=LONG_TIMEOUT)
    assert result.returncode == 0, result.stderr
