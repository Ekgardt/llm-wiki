"""The files that together implement the queue engine, read as one syntax tree without importing it."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
QUEUE_CORE = SCRIPTS / 'queue_core.py'
READER = 'QueueV3CandidateReader'


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding='utf-8'))


def _is_reader(node: ast.stmt) -> bool:
    return isinstance(node, ast.ClassDef) and node.name == READER


def _reader_bases(tree: ast.Module) -> set[str]:
    reader = next(node for node in tree.body if _is_reader(node))
    return {base.id for base in reader.bases if isinstance(base, ast.Name)}


def _module_file(module: str) -> Path:
    return SCRIPTS.joinpath(*module.split('.')).with_suffix('.py')


def _imports_any(node: ast.stmt, names: set[str]) -> bool:
    if not isinstance(node, ast.ImportFrom) or not node.module:
        return False
    return bool(names & {alias.name for alias in node.names})


def _imported_files(tree: ast.Module, names: set[str]) -> set[Path]:
    return {_module_file(node.module) for node in tree.body if _imports_any(node, names)}


def queue_engine_files() -> list[Path]:
    """queue_core, its shared helpers and every file the adopted-queue reader is composed from."""
    tree = _parse(QUEUE_CORE)
    parts = _imported_files(tree, _reader_bases(tree))
    return sorted({QUEUE_CORE, SCRIPTS / 'llm_wiki/queue_store.py', *parts})


def queue_engine_tree() -> ast.Module:
    trees = [_parse(path) for path in queue_engine_files()]
    return ast.Module(body=[node for tree in trees for node in tree.body], type_ignores=[])
