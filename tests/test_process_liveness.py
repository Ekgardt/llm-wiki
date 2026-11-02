"""One probe answers `alive | dead | unknown`; doubt never reads as dead."""

from __future__ import annotations

import ast
import errno
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import process_liveness  # noqa: E402


def test_our_own_process_is_alive() -> None:
    assert process_liveness.process_state(os.getpid()) == "alive"
    assert process_liveness.pid_alive(os.getpid()) is True


@pytest.mark.parametrize("pid", [0, -1, True, "12", None])
def test_a_pid_that_is_not_a_positive_integer_is_unknown_and_never_dead(pid) -> None:
    assert process_liveness.process_state(pid) == "unknown"
    assert process_liveness.pid_alive(pid) is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX kill(2) semantics")
def test_esrch_is_dead_and_eperm_is_alive_in_doubt(monkeypatch) -> None:
    def refuse(_pid, _signal):
        raise PermissionError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(process_liveness.os, "kill", refuse)
    assert process_liveness.process_state(4242) == "unknown"
    assert process_liveness.pid_alive(4242) is True

    def gone(_pid, _signal):
        raise ProcessLookupError(errno.ESRCH, "no such process")

    monkeypatch.setattr(process_liveness.os, "kill", gone)
    assert process_liveness.process_state(4242) == "dead"
    assert process_liveness.pid_alive(4242) is False




def test_linux_identity_binds_scope_and_start_ticks(monkeypatch) -> None:
    stat = b"42 (worker name) S " + b" ".join(str(value).encode() for value in range(1, 23))

    def read(path: Path, _maximum: int) -> bytes:
        if path == Path("/proc/42/stat"):
            return stat
        return b"550e8400-e29b-41d4-a716-446655440000\n"

    monkeypatch.setattr(process_liveness, "_read_bounded_system_file", read)
    monkeypatch.setattr(process_liveness, "_linux_observer_scope", lambda: (
        "-", "550e8400-e29b-41d4-a716-446655440000", "4", "100", "4.200"
    ))

    assert process_liveness._linux_process_start_identity(42) == (
        "linux-v2:-:550e8400-e29b-41d4-a716-446655440000:4:100:4.200:19"
    )


@pytest.mark.parametrize(
    ("system", "probe", "expected"),
    [
        ("Linux", "_linux_process_start_identity", "linux:boot:77"),
        ("Darwin", "_darwin_process_start_identity", "darwin:1:77"),
        ("Windows", "_windows_process_start_identity", "windows:77"),
    ],
)
def test_each_platform_answers_with_its_own_probe(
    monkeypatch, system: str, probe: str, expected: str
) -> None:
    monkeypatch.setattr(process_liveness, "_platform_system", lambda: system)
    monkeypatch.setattr(process_liveness, probe, lambda pid: f"{expected}:{pid}")

    assert process_liveness.process_start_identity(77) == f"{expected}:77"


def test_a_platform_without_a_probe_refuses_by_name(monkeypatch) -> None:
    monkeypatch.setattr(process_liveness, "_platform_system", lambda: "Plan9")

    with pytest.raises(process_liveness.ProcessIdentityUnavailable):
        process_liveness.process_start_identity(77)


def _darwin_information(status: int):
    """What `proc_pidinfo` writes for a process in this state."""

    def fill(_pid, information, size):
        information.pid = 4242
        information.status = status
        information.start_seconds = 1_700_000_000
        information.start_microseconds = 5
        return size

    return fill


def test_a_darwin_zombie_is_a_missing_process_not_an_unsettled_probe(monkeypatch) -> None:
    """Q-L8: with `arg=1` the kernel finds the zombie, and SZOMB reads as gone."""
    monkeypatch.setattr(process_liveness, "_platform_system", lambda: "Darwin")
    monkeypatch.setattr(
        process_liveness,
        "_darwin_proc_pidinfo",
        _darwin_information(process_liveness._DARWIN_SZOMB),
    )

    assert process_liveness.process_start_identity(4242) is None
    assert process_liveness.owner_alive(4242, "darwin:1700000000:5") is False


def test_a_darwin_process_that_runs_keeps_its_start_identity(monkeypatch) -> None:
    monkeypatch.setattr(process_liveness, "_platform_system", lambda: "Darwin")
    monkeypatch.setattr(process_liveness, "_darwin_proc_pidinfo", _darwin_information(2))

    assert process_liveness.process_start_identity(4242) == "darwin:1700000000:5"


def test_a_reused_pid_is_dead_to_the_owner_that_recorded_its_identity(monkeypatch) -> None:
    identity = process_liveness.process_start_identity(os.getpid())
    previous = identity.rsplit(":", 1)[0] + ":1234567890123456789"
    monkeypatch.setattr(process_liveness, "process_start_identity", lambda _pid: identity)

    assert process_liveness.owner_alive(4242, previous) is False
    assert process_liveness.owner_alive(4242, identity) is True


def test_non_linux_owner_without_birth_identity_is_preserved(monkeypatch) -> None:
    """A local PID probe cannot establish an unidentified persisted owner's death."""
    monkeypatch.setattr(process_liveness, "_platform_system", lambda: "Darwin")
    monkeypatch.setattr(process_liveness, "process_state", lambda _pid: "dead")

    assert process_liveness.owner_alive(4242) is True
    assert process_liveness.owner_alive(4242, "") is True


def test_an_unsettled_probe_leaves_the_owner_alive(monkeypatch) -> None:
    def refuse(_pid):
        raise PermissionError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(process_liveness, "process_start_identity", refuse)

    assert process_liveness.owner_alive(4242, "linux:boot:12") is True


def test_our_own_process_matches_the_identity_it_reports() -> None:
    identity = process_liveness.process_start_identity(os.getpid())

    assert process_liveness.owner_alive(os.getpid(), identity) is True


def test_a_pid_beyond_the_platform_range_is_never_alive_by_guess() -> None:
    """Windows answers `dead` (error 87) for 2**40; POSIX raises OverflowError → unknown.
    Either way the one boolean the locks read is the honest one."""
    state = process_liveness.process_state(2**40)
    assert state in {"dead", "unknown"}
    assert process_liveness.pid_alive(2**40) is (state != "dead")


@pytest.mark.parametrize("system", ["Darwin", "Windows"])
@pytest.mark.parametrize("identity", [None, ""])
def test_owner_without_birth_identity_stays_unknown(system, identity, monkeypatch):
    monkeypatch.setattr(process_liveness, "_platform_system", lambda: system)
    monkeypatch.setattr(process_liveness, "process_state", lambda _pid: "dead")
    assert process_liveness.recorded_process_state(4000000, identity) == "unknown"


_ROOT = Path(__file__).resolve().parents[1]


def _import_roots(node):
    if isinstance(node, ast.Import):
        return {alias.name.split('.')[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom):
        return {('.' * node.level + (node.module or '')).split('.')[0]}
    return set()


def _test_helper_import(node, helpers):
    return isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in helpers


def _external_imports(source, helpers=()):
    nodes = [node for node in ast.walk(ast.parse(source)) if not _test_helper_import(node, helpers)]
    roots = set().union(*(_import_roots(node) for node in nodes))
    return roots - sys.stdlib_module_names


def test_process_probe_has_no_executor_dependencies():
    source = (_ROOT / 'scripts/process_liveness.py').read_text(encoding='utf-8')
    assert not _external_imports(source)


def test_owned_tests_do_not_load_executors():
    source = Path(__file__).read_text(encoding='utf-8')
    assert _external_imports(source, {'tests.module_context'}) <= {'process_liveness', 'pytest', 'tiktoken'}


@pytest.mark.parametrize('source', [
    'import memory_state', 'from doctor import _pid_alive',
    'def probe():\n import memory_queue as executor', 'from operational_ownership import *',
    'from . import local', 'from .os import kill',
])
def test_boundary_detects_executor_and_relative_imports(source):
    assert _external_imports(source)


def test_boundary_accepts_standard_library():
    assert not _external_imports('import os as system\nfrom pathlib import Path')


def test_complete_process_liveness_context_fits_user_ceiling():
    manifest = json.loads((_ROOT / 'docs/modules/process-liveness-context.json').read_text())
    paths = manifest['files']
    assert {'scripts/process_liveness.py', 'tests/test_process_liveness.py',
            'docs/modules/process-liveness.md', 'docs/modules/process-liveness-context.json'} <= set(paths)
    assert len(paths) == len(set(paths))
    assert manifest['max_tokens'] == 100_000
    from tests.module_context import measure_context
    total, files = measure_context(_ROOT, _ROOT / 'docs/modules/process-liveness-context.json')
    print(f'process-liveness: {total} tokens; {files} files')


@pytest.mark.parametrize('recorded_scope, observed, expected', [
    (('1' * 64, '550e8400-e29b-41d4-a716-446655440000', '4', '100', '4.200'), None, 'dead'),
    (('1' * 64, '550e8400-e29b-41d4-a716-446655440000', '4', '101', '4.200'), None, 'unknown'),
    (('1' * 64, '550e8400-e29b-41d4-a716-446655440000', '4', '100', '4.201'), None, 'unknown'),
    (('2' * 64, '550e8400-e29b-41d4-a716-446655440000', '4', '100', '4.200'), None, 'unknown'),
    (('1' * 64, '650e8400-e29b-41d4-a716-446655440000', '4', '100', '4.200'), None, 'dead'),
])
def test_recorded_owner_requires_comparable_scope(monkeypatch, recorded_scope, observed, expected):
    scope = ('1' * 64, '550e8400-e29b-41d4-a716-446655440000', '4', '100', '4.200')
    monkeypatch.setattr(process_liveness, '_platform_system', lambda: 'Linux')
    monkeypatch.setattr(process_liveness, '_linux_observer_scope', lambda: scope)
    identity = ':'.join(('linux-v2', *recorded_scope, '10'))
    assert process_liveness.recorded_process_state(42, identity, probe=lambda _pid: observed) == expected


def test_context_helper_is_allowed_only_for_tests():
    source = 'from tests.module_context import measure_context'
    assert _external_imports(source)
    assert not _external_imports(source, {'tests.module_context'})
