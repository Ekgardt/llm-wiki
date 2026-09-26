"""A stored owner is judged by the process it recorded, not by its PID (audit 2026-09-26 C-12).

docs/research/2026-09-26-a-marker-is-judged-by-the-process-it-names.md
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import doctor
import installed_memory_repair
import markdown_transaction
import operational_ownership as ownership
import process_liveness
import pytest

FOREIGN = "test-process:a-number-handed-to-somebody-else"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _own_identity() -> str:
    return str(process_liveness.process_start_identity(os.getpid()))


def _registry_with_marker(tmp_path: Path, identity: str) -> ownership.OwnershipRegistry:
    state_root = tmp_path / "state"
    candidate = state_root / "run/markdown-transactions-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(candidate, source_v2=None)
    (state_root / "run/maintenance.lock").write_bytes(f"{os.getpid()}\n{identity}\n".encode())
    return ownership.OwnershipRegistry(state_root)


def _reclaim(registry: ownership.OwnershipRegistry) -> str:
    return registry.reclaim_dead_marker_owner("nightly", scope="nightly", relative_path="run/maintenance.lock")


def test_an_orphan_marker_whose_pid_went_to_another_process_is_removed(tmp_path: Path) -> None:
    registry = _registry_with_marker(tmp_path, FOREIGN)

    assert _reclaim(registry) == "orphan_removed"


def test_an_orphan_marker_of_a_running_owner_is_kept(tmp_path: Path) -> None:
    registry = _registry_with_marker(tmp_path, _own_identity())

    with pytest.raises(ownership.OperationalOwnershipError, match="owner_busy"):
        _reclaim(registry)


@pytest.mark.parametrize(("identity", "alive"), [(FOREIGN, False), (None, True)])
def test_doctor_reads_an_owner_row_by_its_identity(identity: str | None, alive: bool) -> None:
    assert doctor._owner_pid_live(os.getpid(), identity) is alive


def test_offline_adoption_is_not_blocked_by_a_reused_pid() -> None:
    installed_memory_repair._require_process_absent(os.getpid(), FOREIGN)

    with pytest.raises(ValueError, match="live legacy owner"):
        installed_memory_repair._require_process_absent(os.getpid(), _own_identity())


# The PID-only probes, and the only functions allowed to call them: the probes'
# own wrappers, a child process this process just started, and readers of a
# record that carries no identity (a pre-2026-09-17 preparer record, the v2
# queue's `source_fences`). A new call is refused until it says which it is.
_BARE_PROBES = {"pid_alive", "_pid_alive", "_pid_is_alive", "_is_pid_alive", "_pid_exists"}
_ALLOWED_CALLERS = {
    ("doctor.py", "_owner_pid_live"),
    ("markdown_transaction.py", "_pid_alive"),
    ("markdown_transaction.py", "_preparer_alive"),
    ("maybe_compile.py", "_is_pid_alive"),
    ("memory_queue.py", "_pid_is_alive"),
    ("memory_queue.py", "_descendant_alive"),
    ("memory_queue.py", "_kill_windows_tree"),
    ("memory_queue.py", "MemoryQueue._delete_stale_source_fences"),
    ("memory_state.py", "_is_pid_alive"),
    ("doctor.py", "_pid_alive"),
    ("process_liveness.py", "owner_alive"),
}


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return getattr(node.func, "id", None)


def _identity_used_as_existence(node: ast.AST) -> bool:
    """`process_start_identity(pid) is not None`: the identity read, then thrown away."""
    if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Call):
        return False
    return _called_name(node.left) == "process_start_identity"


def _judges_by_number(node: ast.AST) -> bool:
    if isinstance(node, ast.Call) and _called_name(node) in _BARE_PROBES:
        return True
    return _identity_used_as_existence(node)


def _functions(body: list[ast.stmt], prefix: str = "") -> list[tuple[str, ast.AST]]:
    """Every function with its qualified name, so two classes' methods stay apart."""
    found: list[tuple[str, ast.AST]] = []
    for node in body:
        if isinstance(node, ast.ClassDef):
            found += _functions(node.body, f"{prefix}{node.name}.")
        elif isinstance(node, ast.FunctionDef):
            found.append((prefix + node.name, node))
    return found


def _bare_probe_callers(path: Path) -> set[tuple[str, str]]:
    functions = _functions(ast.parse(path.read_text(encoding="utf-8")).body)
    return {
        (path.name, name) for name, function in functions if any(map(_judges_by_number, ast.walk(function)))
    }


def test_no_new_owner_is_judged_by_its_pid_alone() -> None:
    found = set().union(*(_bare_probe_callers(path) for path in SCRIPTS.glob("*.py")))

    assert found - _ALLOWED_CALLERS == set()
