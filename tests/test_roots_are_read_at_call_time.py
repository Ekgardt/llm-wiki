"""The vault and runtime roots are read from the environment on every call, in one place.

A module that resolves a root at import keeps a copy that outlives the environment
it came from: a test that points the vault at a temporary directory, or a host that
exports `LLM_WIKI_ROOT` after an import, reaches some modules and not others. A
second resolver drifts from the first -- the queue once answered a Git worktree
while the state file answered the main checkout.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from llm_wiki import runtime_roots

REPO = Path(__file__).resolve().parents[1]
ROOT_VARIABLES = {"LLM_WIKI_ROOT", "LLM_WIKI_STATE_ROOT"}
RESOLVER = REPO / "scripts/llm_wiki/runtime_roots.py"


def _product_sources() -> list[Path]:
    return sorted(
        path for path in [*REPO.glob("scripts/**/*.py"), *REPO.glob("benchmark/**/*.py")]
        if path != RESOLVER and "__pycache__" not in path.parts
    )


def _is_roots_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    owner = node.func.value
    return isinstance(owner, ast.Name) and owner.id == "runtime_roots"


def _import_time_nodes(tree: ast.Module):
    """Expressions evaluated when the module is imported."""
    for statement in tree.body:
        yield from _evaluated_at_import(statement)


def _is_main_guard(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.If) or not isinstance(statement.test, ast.Compare):
        return False
    test = statement.test
    return isinstance(test.left, ast.Name) and test.left.id == "__name__"


def _function_header(statement: ast.FunctionDef | ast.AsyncFunctionDef):
    yield from [*statement.args.defaults, *statement.args.kw_defaults, *statement.decorator_list]


def _class_body(statement: ast.ClassDef):
    for inner in statement.body:
        yield from _evaluated_at_import(inner)


_IMPORT_TIME_PARTS = {
    ast.FunctionDef: _function_header,
    ast.AsyncFunctionDef: _function_header,
    ast.ClassDef: _class_body,
}


def _evaluated_at_import(statement: ast.stmt):
    if _is_main_guard(statement):
        return
    parts = _IMPORT_TIME_PARTS.get(type(statement))
    if parts is None:
        yield statement
        return
    yield from parts(statement)


def _local_functions(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        statement.name: statement for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _called_names(node: ast.AST) -> set[str]:
    return {
        call.func.id for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def _root_reaching_functions(tree: ast.Module) -> set[str]:
    """Local functions that call `runtime_roots`, directly or through each other."""
    functions = _local_functions(tree)
    reaching = {name for name, body in functions.items() if _calls_runtime_roots(body)}
    added = reaching
    while added:
        added = _new_callers(functions, reaching)
        reaching |= added
    return reaching


def _calls_runtime_roots(body: ast.AST) -> bool:
    return any(map(_is_roots_call, ast.walk(body)))


def _new_callers(functions: dict[str, ast.AST], reaching: set[str]) -> set[str]:
    return {name for name, body in functions.items() if _called_names(body) & reaching} - reaching


def _reads_a_root(node: ast.AST, reaching: set[str]) -> bool:
    if _is_roots_call(node):
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in reaching


def import_time_root_lines(source: str) -> list[int]:
    tree = ast.parse(source)
    reaching = _root_reaching_functions(tree)
    return [
        node.lineno
        for evaluated in _import_time_nodes(tree) if evaluated is not None
        for node in ast.walk(evaluated) if _reads_a_root(node, reaching)
    ]


def _import_time_root_calls(path: Path) -> list[str]:
    lines = import_time_root_lines(path.read_text(encoding="utf-8"))
    return [f"{path.relative_to(REPO)}:{line}" for line in lines]


def _environment_reads(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        f"{path.relative_to(REPO)}:{node.lineno}"
        for node in ast.walk(tree) if _reads_root_variable(node)
    ]


def _reads_root_variable(node: ast.AST) -> bool:
    if isinstance(node, ast.Subscript):
        return _subscripts_environ(node)
    return isinstance(node, ast.Call) and _calls_environ_reader(node)


def _subscripts_environ(node: ast.Subscript) -> bool:
    return _is_environ(node.value) and isinstance(node.ctx, ast.Load) and _names_root_variable(node.slice)


def _calls_environ_reader(node: ast.Call) -> bool:
    return _is_environ_reader(node.func) and bool(node.args) and _names_root_variable(node.args[0])


def _is_environ_reader(function: ast.AST) -> bool:
    if not isinstance(function, ast.Attribute):
        return False
    return function.attr == "getenv" or (function.attr == "get" and _is_environ(function.value))


def _is_environ(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ"


def _names_root_variable(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in ROOT_VARIABLES


def test_no_product_module_resolves_a_root_at_import():
    offenders = [site for path in _product_sources() for site in _import_time_root_calls(path)]
    assert offenders == []


def test_a_root_read_through_a_local_function_at_import_is_caught():
    source = (
        "def sessions_dir():\n    return runtime_roots.vault_root() / 'sessions'\n"
        "def archive_dir():\n    return sessions_dir() / 'archive'\n"
        "ARCHIVE = archive_dir()\n"
        "def later():\n    return archive_dir()\n"
        "if __name__ == '__main__':\n    later()\n"
    )
    assert import_time_root_lines(source) == [5]


def test_only_runtime_roots_reads_the_root_variables():
    offenders = [site for path in _product_sources() for site in _environment_reads(path)]
    assert offenders == []


def test_a_root_set_after_import_reaches_every_reader(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    import memory_state

    assert runtime_roots.notes_dir() == (tmp_path / "vault/knowledge/notes").resolve()
    assert memory_state.state_file() == (tmp_path / "state/run/state.json").resolve()


@pytest.mark.parametrize("variable", sorted(ROOT_VARIABLES))
def test_an_empty_root_variable_means_unset(monkeypatch, variable):
    monkeypatch.delenv("LLM_WIKI_ROOT", raising=False)
    monkeypatch.delenv("LLM_WIKI_STATE_ROOT", raising=False)
    unset = (runtime_roots.vault_root(), runtime_roots.state_root())
    monkeypatch.setenv(variable, "")
    assert (runtime_roots.vault_root(), runtime_roots.state_root()) == unset


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


WRITERS_IN_A_WORKTREE = """
import sys
from pathlib import Path

import markdown_transaction
import memory_state

main = Path(sys.argv[1])
memory_state.update_state(lambda state: state.update(probe=1))
try:
    markdown_transaction.mutate_knowledge("probe", {main / "knowledge/notes/probe.md": b"# Probe"})
except Exception as exc:
    print(type(exc).__name__, exc)
"""


def _worktree_with_scripts(tmp_path: Path) -> tuple[Path, Path]:
    main, worktree = tmp_path / "main", tmp_path / "worktree"
    (main / "knowledge/notes").mkdir(parents=True)
    _git(main, "init", "-q")
    _git(main, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    _git(main, "worktree", "add", "-q", str(worktree))
    shutil.copytree(REPO / "scripts", worktree / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    return main.resolve(), worktree.resolve()


def test_every_writer_in_a_worktree_reaches_the_main_checkout(tmp_path):
    main, worktree = _worktree_with_scripts(tmp_path)
    environment = {
        key: value for key, value in os.environ.items() if key not in ROOT_VARIABLES
    } | {"PYTHONPATH": str(worktree / "scripts")}
    ran = subprocess.run(
        [sys.executable, "-c", WRITERS_IN_A_WORKTREE, str(main)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120,
    )

    assert (ran.returncode, ran.stdout) == (0, ""), ran.stderr
    assert (main / "run/state.json").is_file()
    assert (main / "knowledge/notes/probe.md").read_bytes() == b"# Probe"
    assert not (worktree / "run").exists()
