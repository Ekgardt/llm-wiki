"""A descriptor this repository writes bytes to is untranslated on every platform.

Windows opens a descriptor in text mode unless told otherwise, and then rewrites
every ``\\n`` it is handed as ``\\r\\n``. The state lock's payload grew a second line
on 2026-09-17, so from that day the lock file on Windows never matched the bytes
its holder remembered, ``_release_state_lock`` never unlinked it, and every writer
after the first timed out. Research:
`docs/research/2026-09-18-a-payload-is-written-as-the-bytes-it-is.md`.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import memory_state  # noqa: E402

from tests.script_sources import script_files, script_key  # noqa: E402

WRITE_FLAGS = frozenset({"O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"})

# The only write descriptors allowed to omit O_BINARY: branches that run on POSIX
# alone, where the flag does not exist and nothing is translated anyway.
POSIX_ONLY_WRITERS = frozenset(
    {
        ("llm_wiki/lsp_process_owner.py", "_publish_record_posix"),
        ("llm_wiki/lsp_process_owner.py", "_publish_lease_posix"),
        ("llm_wiki/integration_transient_files.py", "_open_posix_transient"),
    }
)


# How each kind of node spells a flag name. A `getattr(os, "O_BINARY", 0)` spells
# it as a string constant, which is why constants are read here at all.
_NAME_READERS = {
    ast.Attribute: lambda node: node.attr,
    ast.Name: lambda node: node.id,
    ast.Constant: lambda node: node.value,
}


def _mentioned_name(node: ast.AST) -> str | None:
    """The flag name this node spells: `os.O_RDWR`, `FLAGS`, or `"O_BINARY"`."""
    reader = _NAME_READERS.get(type(node))
    if reader is None:
        return None
    spelled = reader(node)
    return spelled if isinstance(spelled, str) else None


def _flag_names(node: ast.AST) -> set[str]:
    """Every flag name the expression mentions, `getattr` spellings included."""
    found = {_mentioned_name(inner) for inner in ast.walk(node)}
    found.discard(None)
    return found


def _assigned_flags(scope: ast.AST) -> dict[str, set[str]]:
    """Flag names reachable through each name assigned directly in `scope`.

    A nested function is its own scope: one function's `flags` must not lend its
    `O_BINARY` to another function's `flags`, which is how a text-mode Windows
    writer passed this scan on 2026-11-11.
    """
    constants: dict[str, set[str]] = {}
    for node in _scope_nodes(scope):
        target = _single_name_target(node)
        if target is not None:
            constants.setdefault(target, set()).update(_flag_names(node.value))
    return constants


def _scope_nodes(scope: ast.AST) -> list[ast.AST]:
    """Every node inside `scope` that no nested function owns."""
    found: list[ast.AST] = []
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        found.append(node)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))
    return found


def _single_name_target(node: ast.AST) -> str | None:
    target = node.targets[0] if isinstance(node, ast.Assign) else None
    if isinstance(target, ast.Name):
        return target.id
    return None


def _visible_constants(
    call: ast.Call, tree: ast.Module, parents: dict[ast.AST, ast.AST]
) -> dict[str, set[str]]:
    """Module constants, overlaid by those of the function that makes the call."""
    constants = _assigned_flags(tree)
    owner = _owner_node(call, parents)
    if owner is not None:
        constants.update(_assigned_flags(owner))
    return constants


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _owner_node(call: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """The innermost function that holds `call`, or None at module level."""
    node = parents.get(call)
    while node is not None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
        node = parents.get(node)
    return None


def _owner_name(call: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    """The function that holds `call`, or ``<module>`` when none does."""
    owner = _owner_node(call, parents)
    return "<module>" if owner is None else owner.name


def _is_os_open(func: ast.AST) -> bool:
    if not isinstance(func, ast.Attribute) or func.attr != "open":
        return False
    return isinstance(func.value, ast.Name) and func.value.id == "os"


def _open_calls(tree: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_os_open(node.func)
    ]


def _call_flags(call: ast.Call, constants: dict[str, set[str]]) -> set[str]:
    if len(call.args) < 2:
        return set()
    flags = _flag_names(call.args[1])
    for flag in tuple(flags):
        flags |= constants.get(flag, set())
    return flags


def _is_translated_write(call: ast.Call, constants: dict[str, set[str]]) -> bool:
    flags = _call_flags(call, constants)
    if "O_BINARY" in flags:
        return False
    return bool(flags & WRITE_FLAGS)


def _untranslated_write_opens(path: Path) -> set[str]:
    return _untranslated_write_owners(ast.parse(path.read_text(encoding="utf-8")))


def _untranslated_write_owners(tree: ast.Module) -> set[str]:
    parents = _parents(tree)
    return {
        _owner_name(call, parents)
        for call in _open_calls(tree)
        if _is_translated_write(call, _visible_constants(call, tree, parents))
    }


def test_no_write_descriptor_outside_a_posix_branch_is_left_translated() -> None:
    """Every `os.open` that will be written to asks for untranslated bytes."""
    found: set[tuple[str, str]] = set()
    for module in script_files():
        found.update((script_key(module), owner) for owner in _untranslated_write_opens(module))

    assert found == set(POSIX_ONLY_WRITERS)


def test_another_functions_binary_flags_do_not_cover_a_text_mode_writer() -> None:
    """A same-named local elsewhere in the file must not lend its O_BINARY."""
    source = """
import os

def reads(path):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    return os.open(path, flags)

def writes(path):
    flags = os.O_WRONLY | os.O_CREAT
    return os.open(path, flags)

def writes_binary(path):
    flags = os.O_WRONLY | getattr(os, "O_BINARY", 0)
    return os.open(path, flags)
"""
    assert _untranslated_write_owners(ast.parse(source)) == {"writes"}


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "run"
    directory.mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    return directory


def _mark(state: dict) -> None:
    state["written"] = True


def test_the_state_lock_holds_its_payload_and_is_released_afterwards(state_dir) -> None:
    """What `claim_lock` writes is what `_release_state_lock` recognises."""
    payload = memory_state.lock_payload()
    descriptor = memory_state.claim_lock(memory_state.lock_file(), payload)
    os.close(descriptor)
    on_disk = memory_state.lock_file().read_bytes()
    memory_state.lock_file().unlink()

    memory_state.update_state(_mark)

    assert (on_disk, memory_state.lock_file().exists()) == (payload, False)
