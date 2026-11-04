"""Recognizing a real emitted command cannot import installation configuration."""
import subprocess
import sys
from pathlib import Path

from tests.module_boundaries import assert_isolated_import
from tests.slow_machine import LONG_TIMEOUT

ROOT = Path(__file__).resolve().parents[3]
PROGRAM = r"""
import importlib.abc, pathlib, shlex, sys
sys.path.insert(0, sys.argv[1])
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        assert fullname not in {'integration_hook_config', 'install_control', 'reliable_memory'}, fullname
sys.meta_path.insert(0, Guard())
from codex_hook_identity import is_our_codex_command
from llm_wiki.integration_launch_values import codex_provider_bootstrap, codex_provider_payload
root=pathlib.Path(sys.argv[2]).resolve()
args=['uv','run','--locked','--no-sync','--directory',str(root),'python','-c',
      codex_provider_bootstrap(),codex_provider_payload({}),str(root/'scripts/codex_memory.py'),'hook']
assert is_our_codex_command(shlex.join(args))
assert not is_our_codex_command(shlex.join(args[:-1]+['wrong']))
assert is_our_codex_command('uv run scripts/codex_memory.py hook')
assert not is_our_codex_command(None)
"""


def test_value_import_is_independent():
    assert_isolated_import(ROOT, 'llm_wiki.integration_launch_values', dependencies=('llm_wiki',))


def test_real_command_recognition_cannot_import_installer(tmp_path):
    result = subprocess.run([sys.executable, '-c', PROGRAM, str(ROOT/'scripts'), str(tmp_path)],
                            capture_output=True, text=True, timeout=LONG_TIMEOUT)
    assert result.returncode == 0, result.stderr
