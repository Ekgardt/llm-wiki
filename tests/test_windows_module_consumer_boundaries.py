"""Consumer integration assertions retained from leaf module qualification."""
import ast
from pathlib import Path

from tests.modules.windows_acl.boundary_helpers import NAMES as _windows_acl_NAMES
from tests.queue_engine_sources import queue_engine_files
from tests.script_sources import split_module_files

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def coordinator_source_files() -> list[Path]:
    return split_module_files('markdown_transaction', 'markdown')


def _executor_files() -> list[Path]:
    return [SCRIPTS / 'memory_queue.py', *queue_engine_files(), *coordinator_source_files()]


def _top_level_names(path: Path, kinds: tuple[type, ...]) -> set[str]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    return {n.name for n in tree.body if isinstance(n, kinds)}


def test_windows_acl_executors_do_not_define_acl_implementation():
    for path in _executor_files():
        assert not _top_level_names(path, (ast.FunctionDef,)).intersection(_windows_acl_NAMES), path.name


def _is_executor_module(module: str | None) -> bool:
    executors = {'markdown_transaction', 'memory_queue', 'queue_core'}
    return module in executors or (module or '').startswith(('llm_wiki.markdown_', 'llm_wiki.queue_reader'))


def test_windows_acl_runtime_io_does_not_import_executors_for_permissions():
    tree = ast.parse((ROOT / 'scripts/reliable_memory.py').read_text(encoding='utf-8'))
    sources = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not [module for module in sources if _is_executor_module(module)]


_windows_native_NAMES = {'WindowsFileInformation', 'WindowsDispositionInformation', 'windows_kernel32'}


def test_windows_native_executor_no_longer_defines_native_binding():
    for path in coordinator_source_files():
        assert not _top_level_names(path, (ast.FunctionDef, ast.ClassDef)).intersection(_windows_native_NAMES), path.name
