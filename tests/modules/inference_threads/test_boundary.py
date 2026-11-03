"""The existing inference_threads owner remains independent of product executors."""
from pathlib import Path

from tests.module_boundaries import assert_isolated_import, assert_stdlib_boundary

ROOT = Path(__file__).resolve().parents[3]


def test_owner_imports_only_standard_library():
    assert_stdlib_boundary(ROOT, 'inference_threads')
    assert_isolated_import(ROOT, 'inference_threads')


def test_import_does_not_start_threads_or_register_exit_work():
    import subprocess
    import sys

    from tests.slow_machine import LONG_TIMEOUT

    program = '''
import atexit, sys, threading
sys.path.insert(0, sys.argv[1])
def forbidden(*args, **kwargs):
    raise AssertionError('import attempted lifecycle work')
atexit.register = forbidden
threading.Thread.start = forbidden
import inference_threads
assert inference_threads.running() == []
assert not inference_threads.stopping.is_set()
'''
    result = subprocess.run([sys.executable, '-c', program, str(ROOT / 'scripts')],
                            capture_output=True, text=True, timeout=LONG_TIMEOUT)
    assert result.returncode == 0, result.stderr
