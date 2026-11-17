"""Bounded no-follow probing of an operator's private corpus by the Python navigation benchmark."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent / "benchmark"
SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
for path in (BENCHMARK_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import navigation_benchmark_deadlines as deadlines  # noqa: E402
import navigation_benchmark_operator_corpus as operator_corpus  # noqa: E402
from code_intelligence import Capability  # noqa: E402
from code_navigation import (  # noqa: E402
    NavigationResult,
    NavigationStatus,
)
from navigation_benchmark_git import (  # noqa: E402
    initialize_deterministic_git,
)
from navigation_benchmark_operator_corpus import _probe_operator_corpus  # noqa: E402

from tests.code_navigation_benchmark_runtime_helpers import (  # noqa: E402
    _empty_navigation_result,
    _qualified_identity,
    _result_with_locations,
)
from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


def _install_operator_probe_fakes(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> None:
    scope = SimpleNamespace(
        checkout_root=str(root),
        repository_id="repository:test",
        checkout_id="checkout:test",
    )

    class OperatorNavigation:
        def __init__(self, scope: object, session: object, identity: object) -> None:
            pass

        def query(self, request: object, *, deadline: float) -> NavigationResult:
            return _result_with_locations(request, NavigationStatus.OK, ())

        def close(self, *, deadline: float) -> None:
            return None

    monkeypatch.setattr(operator_corpus, "resolve_repository_scope", lambda value: scope)
    monkeypatch.setattr(
        operator_corpus,
        "discover_pyright",
        lambda scope, state_root, deadline: _qualified_identity(),
    )
    monkeypatch.setattr(
        operator_corpus,
        "PyrightSession",
        lambda scope, identity, state_root: object(),
    )
    monkeypatch.setattr(operator_corpus, "CodeNavigation", OperatorNavigation)


def test_operator_probe_never_reads_or_reports_outside_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-symlink"
    outside = tmp_path / "outside-sentinel.py"
    root.mkdir()
    (root / "inside.py").write_text("def inside() -> int:\n    return 1\n", encoding="utf-8")
    outside.write_text("OUTSIDE_PRIVATE_SENTINEL", encoding="utf-8")
    try:
        os.symlink(outside, root / "outside-link.py")
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {type(exc).__name__}")
    _install_operator_probe_fakes(monkeypatch, root)
    visited: list[Path] = []

    def definition(
        path: Path,
        checkout: Path,
        *,
        operator_root: Path,
        deadline: float,
    ):
        assert path.resolve() != outside.resolve()
        visited.append(path)
        return None

    monkeypatch.setattr(operator_corpus, "_operator_definition", definition)
    metrics = _probe_operator_corpus(
        root,
        tmp_path / "state",
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )

    assert [path.name for path in visited] == ["inside.py"]
    assert metrics == {
        "available": True,
        "python_files": 1,
        "queries_attempted": 0,
        "queries_succeeded": 0,
        "errors": 0,
    }
    encoded = json.dumps(metrics, sort_keys=True)
    assert outside.name not in encoded
    assert "OUTSIDE_PRIVATE_SENTINEL" not in encoded


def test_operator_probe_fails_closed_without_leaking_oversized_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-oversized"
    root.mkdir()
    private_text = b"PRIVATE_OVERSIZED_SOURCE_TEXT"
    source = root / "private_project_name.py"
    source.write_bytes(
        b"def oversized() -> int:\n    return 1\n# "
        + private_text
        + b"x" * operator_corpus.OPERATOR_MAX_SOURCE_BYTES
        + b"\n"
    )
    _install_operator_probe_fakes(monkeypatch, root)

    metrics = _probe_operator_corpus(
        root,
        tmp_path / "state",
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )
    encoded = json.dumps(metrics, sort_keys=True)

    assert metrics == {
        "available": False,
        "python_files": 1,
        "queries_attempted": 0,
        "queries_succeeded": 0,
        "errors": 1,
    }
    assert source.name not in encoded
    assert private_text.decode() not in encoded


def test_operator_source_uses_bounded_chunk_reads_not_path_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-bounded-read"
    root.mkdir()
    source = root / "bounded.py"
    source.write_bytes(
        b"def bounded() -> int:\n    return 1\n# "
        + b"x" * (operator_corpus.OPERATOR_READ_CHUNK_BYTES * 2)
        + b"\n"
    )
    real_read_bytes = Path.read_bytes
    real_read = os.read
    requested_sizes: list[int] = []

    def forbid_path_read_bytes(path: Path) -> bytes:
        if path == source:
            pytest.fail("operator source must not use Path.read_bytes")
        return real_read_bytes(path)

    def tracked_read(descriptor: int, size: int) -> bytes:
        requested_sizes.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(Path, "read_bytes", forbid_path_read_bytes)
    monkeypatch.setattr(operator_corpus.os, "read", tracked_read)
    definition = operator_corpus._operator_definition(
        source,
        root,
        operator_root=root,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )

    assert definition == ("bounded.py", 1, len("def "))
    assert len(requested_sizes) >= 3
    assert max(requested_sizes) <= operator_corpus.OPERATOR_READ_CHUNK_BYTES


def test_operator_source_deadline_stops_chunk_reads_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-read-deadline"
    root.mkdir()
    source = root / "deadline.py"
    source.write_bytes(
        b"def deadline() -> int:\n    return 1\n# "
        + b"x" * (operator_corpus.OPERATOR_READ_CHUNK_BYTES * 3)
        + b"\n"
    )
    real_read = os.read
    expired = False
    reads = 0

    def tracked_read(descriptor: int, size: int) -> bytes:
        nonlocal expired, reads
        reads += 1
        chunk = real_read(descriptor, size)
        expired = True
        return chunk

    def check_deadline(deadline: float, **kwargs) -> float:
        if expired:
            raise deadlines.BenchmarkTimeoutError("bounded source read expired")
        return 0.0

    monkeypatch.setattr(operator_corpus.os, "read", tracked_read)
    monkeypatch.setattr(operator_corpus, "_check_run_deadline", check_deadline)
    with pytest.raises(deadlines.BenchmarkTimeoutError):
        operator_corpus._operator_definition(
            source,
            root,
            operator_root=root,
            deadline=time.monotonic() + 10.0,
        )
    assert reads == 1


def test_operator_source_revalidates_stable_fstat_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-fstat"
    root.mkdir()
    source = root / "stable.py"
    source.write_text("def stable() -> int:\n    return 1\n", encoding="utf-8")
    real_fstat = os.fstat
    calls = 0

    def changing_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        info = real_fstat(descriptor)
        if calls == 2:
            return SimpleNamespace(
                st_dev=info.st_dev,
                st_ino=info.st_ino,
                st_mode=info.st_mode,
                st_size=info.st_size,
                st_mtime_ns=info.st_mtime_ns + 1,
                st_ctime_ns=info.st_ctime_ns,
            )
        return info

    monkeypatch.setattr(operator_corpus.os, "fstat", changing_fstat)
    with pytest.raises(PermissionError, match="changed during read"):
        operator_corpus._operator_definition(
            source,
            root,
            operator_root=root,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
    assert calls == 2


def test_operator_source_rejects_outside_file_before_reading_private_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-contained"
    root.mkdir()
    outside = tmp_path / "PRIVATE_OUTSIDE.py"
    outside.write_text("PRIVATE_OUTSIDE_SENTINEL", encoding="utf-8")
    real_read_bytes = Path.read_bytes

    def forbid_outside_read(path: Path) -> bytes:
        if path == outside:
            pytest.fail("outside source was read")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", forbid_outside_read)
    with pytest.raises(PermissionError, match="outside operator root") as captured:
        operator_corpus._operator_definition(
            outside,
            root,
            operator_root=root,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
    assert outside.name not in str(captured.value)
    assert "PRIVATE_OUTSIDE_SENTINEL" not in str(captured.value)


def test_operator_source_rejects_leaf_symlink_without_reading_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-leaf-link"
    root.mkdir()
    outside = tmp_path / "PRIVATE_LEAF_TARGET.py"
    outside.write_text("PRIVATE_LEAF_SENTINEL", encoding="utf-8")
    link = root / "linked.py"
    try:
        os.symlink(outside, link)
    except OSError as exc:
        pytest.skip(f"file symlink creation is unavailable: {type(exc).__name__}")
    real_read_bytes = Path.read_bytes

    def forbid_target_read(path: Path) -> bytes:
        if path in {link, outside}:
            pytest.fail("linked outside source was read")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", forbid_target_read)
    with pytest.raises(PermissionError, match="regular file") as captured:
        operator_corpus._operator_definition(
            link,
            root,
            operator_root=root,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )
    assert outside.name not in str(captured.value)
    assert "PRIVATE_LEAF_SENTINEL" not in str(captured.value)


def test_operator_source_rejects_reparse_leaf_metadata_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-reparse-leaf"
    root.mkdir()
    source = root / "reparse.py"
    source.write_text("def reparse() -> int:\n    return 1\n", encoding="utf-8")
    real_stat = Path.stat

    def reparse_stat(path: Path, *, follow_symlinks: bool = True):
        info = real_stat(path, follow_symlinks=follow_symlinks)
        if path == source and not follow_symlinks:
            return SimpleNamespace(
                st_dev=info.st_dev,
                st_ino=info.st_ino,
                st_mode=info.st_mode,
                st_size=info.st_size,
                st_mtime_ns=info.st_mtime_ns,
                st_ctime_ns=info.st_ctime_ns,
                st_file_attributes=0x400,
            )
        return info

    monkeypatch.setattr(Path, "stat", reparse_stat)
    with pytest.raises(PermissionError, match="regular file"):
        operator_corpus._operator_definition(
            source,
            root,
            operator_root=root,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows safe-handle coverage")
def test_operator_source_uses_windows_no_follow_safe_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-windows-handle"
    root.mkdir()
    source = root / "safe.py"
    source.write_text("def safe() -> int:\n    return 1\n", encoding="utf-8")
    opened: list[Path] = []
    real_open = operator_corpus._windows_workspace.open_exclusive_readonly_source_file

    def tracked_open(path: Path) -> int:
        opened.append(path)
        return real_open(path)

    monkeypatch.setattr(
        operator_corpus._windows_workspace,
        "open_exclusive_readonly_source_file",
        tracked_open,
    )
    definition = operator_corpus._operator_definition(
        source,
        root,
        operator_root=root,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )

    assert definition == ("safe.py", 1, len("def "))
    assert opened == [source]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction coverage")
def test_operator_probe_never_descends_into_windows_junction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-junction"
    outside = tmp_path / "outside-junction"
    root.mkdir()
    outside.mkdir()
    (root / "inside.py").write_text("def inside() -> int:\n    return 1\n", encoding="utf-8")
    sentinel = outside / "outside.py"
    sentinel.write_text("OUTSIDE_JUNCTION_SENTINEL", encoding="utf-8")
    junction = root / "junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        capture_output=True,
        shell=False,
        timeout=10,
    )
    if created.returncode != 0:
        pytest.skip("junction creation is unavailable")
    _install_operator_probe_fakes(monkeypatch, root)
    visited: list[Path] = []

    def definition(
        path: Path,
        checkout: Path,
        *,
        operator_root: Path,
        deadline: float,
    ):
        assert outside.resolve() not in path.resolve().parents
        visited.append(path)
        return None

    monkeypatch.setattr(operator_corpus, "_operator_definition", definition)
    metrics = _probe_operator_corpus(
        root,
        tmp_path / "state",
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )

    assert [path.name for path in visited] == ["inside.py"]
    assert metrics["python_files"] == 1
    assert metrics["available"] is True


def test_operator_traversal_entry_cap_fails_closed_before_provider_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-entry-cap"
    root.mkdir()
    for index in range(3):
        (root / f"module_{index}.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(operator_corpus, "OPERATOR_MAX_SCANNED_ENTRIES", 1)
    monkeypatch.setattr(
        operator_corpus,
        "discover_pyright",
        lambda *args, **kwargs: pytest.fail("provider must not start after traversal cap"),
    )

    metrics = _probe_operator_corpus(
        root,
        tmp_path / "state",
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )

    assert metrics["available"] is False
    assert metrics["errors"] == 1
    assert metrics["queries_attempted"] == 0


def test_operator_traversal_depth_cap_fails_closed_before_provider_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-depth-cap"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "module.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(operator_corpus, "OPERATOR_MAX_DEPTH", 0)
    monkeypatch.setattr(
        operator_corpus,
        "discover_pyright",
        lambda *args, **kwargs: pytest.fail("provider must not start after traversal cap"),
    )

    metrics = _probe_operator_corpus(
        root,
        tmp_path / "state",
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )

    assert metrics["available"] is False
    assert metrics["errors"] == 1
    assert metrics["queries_attempted"] == 0


def test_operator_traversal_deadline_propagates_benchmark_timeout(
    tmp_path: Path,
) -> None:
    root = tmp_path / "operator-deadline"
    root.mkdir()
    (root / "module.py").write_text("def value():\n    return 1\n", encoding="utf-8")

    with pytest.raises(deadlines.BenchmarkTimeoutError):
        _probe_operator_corpus(
            root,
            tmp_path / "state",
            deadline=time.monotonic() - 1.0,
        )


def test_operator_traversal_selects_a_deterministic_bounded_file_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "operator-file-cap"
    root.mkdir()
    for name in ("z.py", "a.py", "m.py", "b.py"):
        (root / name).write_text("def value():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(operator_corpus, "OPERATOR_MAX_PYTHON_FILES", 2)
    _install_operator_probe_fakes(monkeypatch, root)
    visited: list[str] = []

    def definition(
        path: Path,
        checkout: Path,
        *,
        operator_root: Path,
        deadline: float,
    ):
        visited.append(path.name)
        return None

    monkeypatch.setattr(operator_corpus, "_operator_definition", definition)
    metrics = _probe_operator_corpus(
        root,
        tmp_path / "state",
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )

    assert visited == ["a.py", "b.py"]
    assert metrics["python_files"] == 2
    assert metrics["available"] is True


def test_operator_probe_close_failure_marks_metrics_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator_root = tmp_path / "operator-close"
    operator_root.mkdir()
    (operator_root / "sample.py").write_text(
        "def sample() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    initialize_deterministic_git(operator_root)

    close_deadlines: list[float] = []

    class FailingOperatorNavigation:
        def __init__(self, scope: object, session: object, identity: object) -> None:
            self.scope = scope

        def query(self, request: object, *, deadline: float) -> NavigationResult:
            return replace(
                _empty_navigation_result(request),
                status=NavigationStatus.OK,
                effective_capability=Capability.DEFINITIONS,
            )

        def close(self, *, deadline: float) -> None:
            close_deadlines.append(deadline)
            if len(close_deadlines) == 1:
                raise RuntimeError("closed operator close failure")

    monkeypatch.setattr(
        operator_corpus,
        "discover_pyright",
        lambda scope, state_root, deadline: _qualified_identity(),
    )
    monkeypatch.setattr(
        operator_corpus,
        "PyrightSession",
        lambda scope, identity, state_root: object(),
    )
    monkeypatch.setattr(
        operator_corpus,
        "CodeNavigation",
        FailingOperatorNavigation,
    )

    metrics = _probe_operator_corpus(
        operator_root,
        tmp_path / "state",
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )
    assert len(close_deadlines) == 2
    assert close_deadlines[1] >= close_deadlines[0]
    assert metrics == {
        "available": False,
        "python_files": 1,
        "queries_attempted": 1,
        "queries_succeeded": 1,
        "errors": 1,
    }
