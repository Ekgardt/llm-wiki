"""Contained repository paths, and the scripts a source loads by their file path."""
import ast
import functools
import posixpath
from pathlib import Path

PATH_LOADED_ROOTS = ('scripts', 'benchmark')
PATH_LOADERS = frozenset({'spec_from_file_location', 'run_path', 'SourceFileLoader'})


def _contained(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    path.relative_to(root.resolve())
    return path


def syntax_nodes(source: Path) -> list[ast.AST]:
    """A file Python cannot parse can never be imported, so it imports nothing either."""
    try:
        return list(ast.walk(ast.parse(source.read_bytes())))
    except SyntaxError:
        return []


@functools.cache
def _scripts(root: Path) -> frozenset[Path]:
    return frozenset(path.resolve() for folder in PATH_LOADED_ROOTS for path in (root / folder).rglob('*.py'))


@functools.cache
def _scripts_by_unique_name(root: Path) -> dict[str, Path]:
    found: dict[str, set[Path]] = {}
    for path in _scripts(root):
        found.setdefault(path.name, set()).add(path)
    return {name: next(iter(paths)) for name, paths in found.items() if len(paths) == 1}


@functools.cache
def _scripts_by_repository_path(root: Path) -> dict[str, Path]:
    return {path.relative_to(root.resolve()).as_posix(): path for path in _scripts(root)}


def _named_scripts(root: Path, name: str) -> set[Path]:
    """A bare name several scripts share (such as `__init__.py`) identifies none of them."""
    if '/' in name:
        return _known_script(_scripts_by_repository_path(root), posixpath.normpath(name))
    return _known_script(_scripts_by_unique_name(root), name)


def _known_script(scripts: dict[str, Path], key: str) -> set[Path]:
    found = scripts.get(key)
    if found is None:
        return set()
    return {found}


def path_loaded_sources(root: Path, nodes: list[ast.AST]) -> set[Path]:
    """A source that loads modules by file path imports every script it names.

    Statically the name cannot be tied to one loader call, so every script the source
    names counts. A name in a subprocess command crosses a command-line boundary, and a
    path read as data or as text to check a rule is not executed, so a source without
    an in-process path loader requires none of the scripts it names.
    """
    if not any(_is_path_loader_call(node) for node in nodes):
        return set()
    return set().union(*(_named_scripts(root, name) for name in _python_file_names(nodes)))


def _is_path_loader_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    return _called_name(node.func) in PATH_LOADERS


def _called_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, 'id', None)


def _python_file_names(nodes: list[ast.AST]) -> set[str]:
    constants = [node.value for node in nodes if isinstance(node, ast.Constant)]
    return {text for text in constants if _python_file_name(text)}


def _python_file_name(value: object) -> bool:
    return isinstance(value, str) and value.endswith('.py')
