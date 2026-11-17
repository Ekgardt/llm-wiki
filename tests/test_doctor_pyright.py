"""The doctor's Pyright check: identity, digest, scope, deadlines and how it feeds the run-deletion verdict."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import generation_maintenance
import pytest

from tests.doctor_case_helpers import (
    _junction_or_skip,
    give_doctor_a_generous_budget,
)
from tests.doctor_helpers import (
    _build_root,
    _check,
    _qualified_pyright_check,
)
from tests.slow_machine import SHORT_TIMEOUT

_budget_that_survives_a_slow_runner = pytest.fixture(autouse=True)(give_doctor_a_generous_budget)


def _broken_directory_link_or_skip(link: Path, target: Path) -> None:
    if os.name == "nt":
        target.mkdir()
        _junction_or_skip(link, target)
        try:
            target.rmdir()
        except OSError as exc:
            pytest.skip(f"could not make a dangling Windows junction: {exc}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")


def _missing_pyright_identity():
    import pyright_profile

    return pyright_profile.PyrightIdentity(
        status="missing",
        source=None,
        version=None,
        node_executable=None,
        node_version=None,
        node_major=None,
        server_executable=None,
        executable_sha256=None,
        package_sha256=None,
        initialization_options_sha256="a" * 64,
        configuration_sha256="b" * 64,
        qualified=False,
        degradation_codes=("pyright_missing",),
    )


def _mismatched_pyright_identity():
    import pyright_profile

    return pyright_profile.PyrightIdentity(
        status="degraded",
        source="system",
        version="1.1.400",
        node_executable=None,
        node_version=None,
        node_major=20,
        server_executable=None,
        executable_sha256=None,
        package_sha256=None,
        initialization_options_sha256=pyright_profile.PYRIGHT_INITIALIZATION_OPTIONS_SHA256,
        configuration_sha256=pyright_profile.PYRIGHT_CONFIGURATION_SHA256,
        qualified=False,
        degradation_codes=("pyright_version_mismatch",),
    )


def _qualified_pyright_identity():
    import pyright_profile

    return pyright_profile.PyrightIdentity(
        status="qualified",
        source="project-local",
        version="1.1.411",
        node_executable=Path("node"),
        node_version="v20.19.0",
        node_major=20,
        server_executable=Path("langserver.index.js"),
        executable_sha256="c" * 64,
        package_sha256="d" * 64,
        initialization_options_sha256=pyright_profile.PYRIGHT_INITIALIZATION_OPTIONS_SHA256,
        configuration_sha256=pyright_profile.PYRIGHT_CONFIGURATION_SHA256,
        qualified=True,
        degradation_codes=(),
    )


def test_doctor_reports_missing_pyright(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_lsp

    monkeypatch.setattr(
        "repository_scope.resolve_repository_scope", lambda root, *, deadline: object()
    )
    monkeypatch.setattr(
        "pyright_profile.discover_pyright",
        lambda *a, **k: _missing_pyright_identity(),
    )
    check = doctor_lsp.pyright_check(tmp_path, tmp_path, deadline=time.monotonic() + SHORT_TIMEOUT)
    # Never installed is an optional feature not taken, not a fault (2026-09-27,
    # docs/research/2026-09-27-what-is-not-yet-due-is-not-a-warning.md).
    assert check["status"] == "skipped"
    assert check["details"]["status"] == "missing"
    assert check["details"]["codes"] == ["pyright_missing"]
    assert "install_pyright" in check["details"]["recommended_action"]


def test_doctor_pyright_passes_deadline_to_repository_scope(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_lsp

    deadline = time.monotonic() + 10
    observed: list[tuple[Path, float]] = []
    scope = object()

    def resolve(root: Path, *, deadline: float):
        observed.append((root, deadline))
        return scope

    monkeypatch.setattr("repository_scope.resolve_repository_scope", resolve)
    monkeypatch.setattr(
        "pyright_profile.discover_pyright",
        lambda repository, **kwargs: _missing_pyright_identity(),
    )

    doctor_lsp.pyright_check(tmp_path, tmp_path, deadline=deadline)

    assert observed == [(tmp_path, deadline)]


def test_doctor_pyright_maps_infinite_deadline_to_api_none(tmp_path, monkeypatch) -> None:
    import pyright_profile
    import repository_scope
    from llm_wiki import doctor_lsp

    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)

    observed: list[tuple[str, float | None]] = []
    real_resolve = repository_scope.resolve_repository_scope
    real_discover = pyright_profile.discover_pyright
    no_candidates = pyright_profile.PyrightCandidates((), (), ())

    def resolve(root: Path, *, deadline: float | None):
        observed.append(("scope", deadline))
        return real_resolve(root, deadline=deadline)

    def discover(repository, *, state_root: Path, deadline: float | None):
        observed.append(("discovery", deadline))
        return real_discover(
            repository,
            state_root=state_root,
            candidates=no_candidates,
            deadline=deadline,
        )

    monkeypatch.setattr(repository_scope, "resolve_repository_scope", resolve)
    monkeypatch.setattr(pyright_profile, "discover_pyright", discover)

    check = doctor_lsp.pyright_check(tmp_path, tmp_path, deadline=float("inf"))

    assert observed == [("scope", None), ("discovery", None)]
    assert check["details"]["status"] == "missing"
    assert check["details"]["codes"] == ["pyright_missing"]


def test_doctor_pyright_reports_executable_digest(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_lsp

    monkeypatch.setattr(
        "repository_scope.resolve_repository_scope", lambda root, *, deadline: object()
    )
    monkeypatch.setattr(
        "pyright_profile.discover_pyright",
        lambda repository, **kwargs: _qualified_pyright_identity(),
    )

    check = doctor_lsp.pyright_check(tmp_path, tmp_path, deadline=float("inf"))

    assert check["details"]["executable_sha256"] == "c" * 64


def test_doctor_pyright_surfaces_executable_digest_mismatch(tmp_path, monkeypatch) -> None:
    from dataclasses import replace

    from llm_wiki import (
        doctor_lsp,
    )

    identity = replace(
        _qualified_pyright_identity(),
        status="degraded",
        source="managed",
        qualified=False,
        degradation_codes=("pyright_executable_digest_mismatch",),
    )
    monkeypatch.setattr(
        "repository_scope.resolve_repository_scope", lambda root, *, deadline: object()
    )
    monkeypatch.setattr(
        "pyright_profile.discover_pyright",
        lambda repository, **kwargs: identity,
    )

    check = doctor_lsp.pyright_check(tmp_path, tmp_path, deadline=float("inf"))

    assert check["status"] == "degraded"
    assert check["details"]["codes"] == ["pyright_executable_digest_mismatch"]
    assert check["details"]["executable_sha256"] == "c" * 64


def test_doctor_pyright_reports_exact_degradation_codes_and_fields(tmp_path, monkeypatch) -> None:
    import pyright_profile
    from llm_wiki import doctor_lsp

    codes = (
        "pyright_configuration_mismatch",
        "pyright_node_major_mismatch",
        "pyright_package_mismatch",
    )
    identity = pyright_profile.PyrightIdentity(
        status="degraded",
        source="managed",
        version="1.1.411",
        node_executable=Path("node"),
        node_version="v18.20.0",
        node_major=18,
        server_executable=Path("langserver.index.js"),
        executable_sha256="e" * 64,
        package_sha256="f" * 64,
        initialization_options_sha256="1" * 64,
        configuration_sha256="2" * 64,
        qualified=False,
        degradation_codes=codes,
    )
    monkeypatch.setattr(
        "repository_scope.resolve_repository_scope", lambda root, *, deadline: object()
    )
    monkeypatch.setattr(
        "pyright_profile.discover_pyright",
        lambda repository, **kwargs: identity,
    )

    check = doctor_lsp.pyright_check(tmp_path, tmp_path, deadline=time.monotonic() + SHORT_TIMEOUT)

    assert check["status"] == "degraded"
    assert check["details"] == {
        "status": "degraded",
        "source": "managed",
        "version": "1.1.411",
        "node_major": 18,
        "node_version": "v18.20.0",
        "package_sha256": "f" * 64,
        "executable_sha256": "e" * 64,
        "initialization_options_sha256": "1" * 64,
        "configuration_sha256": "2" * 64,
        "qualified": False,
        "codes": list(codes),
        "executable_sha256_present": True,
        "recommended_action": (
            "uv run python scripts/install_pyright.py --state-root <state-root>"
        ),
    }


def test_doctor_pyright_reports_scope_timeout_separately(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_lsp

    def resolve(root: Path, *, deadline: float):
        raise TimeoutError("scope deadline")

    monkeypatch.setattr("repository_scope.resolve_repository_scope", resolve)

    check = doctor_lsp.pyright_check(tmp_path, tmp_path, deadline=time.monotonic() + 10)

    assert check["status"] == "degraded"
    assert check["details"]["status"] == "timeout"
    assert check["details"]["codes"] == ["pyright_timeout"]


def test_doctor_pyright_reports_unsafe_scope_separately(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_lsp
    from repository_scope import RepositoryScopeUnavailable

    def resolve(root: Path, *, deadline: float):
        raise RepositoryScopeUnavailable("unsafe scope")

    monkeypatch.setattr("repository_scope.resolve_repository_scope", resolve)

    check = doctor_lsp.pyright_check(tmp_path, tmp_path, deadline=time.monotonic() + SHORT_TIMEOUT)

    assert check["status"] == "degraded"
    assert check["details"]["status"] == "unsafe"
    assert check["details"]["codes"] == ["pyright_unsafe"]


def test_doctor_pyright_reports_unsafe_managed_discovery_separately(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_lsp

    monkeypatch.setattr(
        "repository_scope.resolve_repository_scope", lambda root, *, deadline: object()
    )

    def discover(repository, **kwargs):
        raise PermissionError("managed Pyright root is unsafe")

    monkeypatch.setattr("pyright_profile.discover_pyright", discover)

    check = doctor_lsp.pyright_check(tmp_path, tmp_path, deadline=time.monotonic() + SHORT_TIMEOUT)

    assert check["status"] == "degraded"
    assert check["details"]["status"] == "unsafe"
    assert check["details"]["codes"] == ["pyright_unsafe"]


def test_run_doctor_checks_pyright_without_managed_or_runtime_dirs(tmp_path, monkeypatch) -> None:
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, state_root, home = _build_root(tmp_path)
    calls: list[tuple[Path, Path]] = []

    def pyright_check(root: Path, state_root: Path, *, deadline: float) -> dict:
        calls.append((root, state_root))
        return generation_maintenance.result(
            "pyright",
            "degraded",
            "Pyright is missing.",
            {"status": "missing", "codes": ["pyright_missing"]},
        )

    monkeypatch.setattr(doctor_lsp, "pyright_check", pyright_check)

    report = doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        time_budget_seconds=30,
    )

    assert calls == [(root, state_root)]
    assert _check(report, "pyright")["details"]["codes"] == ["pyright_missing"]


def test_run_doctor_broken_lsp_link_blocks_run_deletion(tmp_path, monkeypatch) -> None:
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, state_root, home = _build_root(tmp_path)
    lsp_root = state_root / "run" / "lsp"
    target = tmp_path / "removed-lsp-target"
    _broken_directory_link_or_skip(lsp_root, target)
    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)

    report = doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        time_budget_seconds=30,
    )
    lsp = _check(report, "lsp")

    assert lsp["status"] == "degraded"
    assert "lsp_state_unreadable" in lsp["details"]["codes"]
    assert report["run_deletion"]["blockers"] == [{"code": "legacy_protocol_unquiesced"}]


def test_run_doctor_reuses_collected_lsp_check_for_deletion(tmp_path, monkeypatch) -> None:
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, state_root, home = _build_root(tmp_path)
    calls: list[Path] = []

    def lsp_check(state_root: Path, now: datetime, *, deadline: float) -> dict:
        calls.append(state_root)
        if len(calls) > 1:
            raise AssertionError("LSP runtime was scanned twice")
        return generation_maintenance.result(
            "lsp",
            "ok",
            "No LSP runtime owners are present.",
            {
                "codes": [],
                "owners": [],
                "deletion_codes": [],
                "read_error": False,
            },
        )

    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)
    monkeypatch.setattr(doctor_lsp, "lsp_runtime_check", lsp_check)

    doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        time_budget_seconds=30,
    )

    assert calls == [state_root]


def test_run_doctor_executes_lsp_check_after_budget_exhaustion(tmp_path, monkeypatch) -> None:
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, state_root, home = _build_root(tmp_path)
    calls: list[float] = []
    real_lsp_check = doctor_lsp.lsp_runtime_check

    def lsp_check(state_root: Path, now: datetime, *, deadline: float) -> dict:
        calls.append(deadline)
        return real_lsp_check(state_root, now, deadline=deadline)

    monkeypatch.setattr(doctor_lsp, "lsp_runtime_check", lsp_check)
    deadline = time.monotonic() - 1

    report = doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        deadline=deadline,
    )

    assert calls == [deadline]
    assert _check(report, "lsp")["details"]["codes"] == ["lsp_state_unreadable"]
    assert report["run_deletion"]["blockers"] == [{"code": "run_deletion_state_unknown"}]


def test_doctor_reports_mismatched_pyright(tmp_path, monkeypatch) -> None:
    from llm_wiki import doctor_lsp

    monkeypatch.setattr(
        "repository_scope.resolve_repository_scope", lambda root, *, deadline: object()
    )
    monkeypatch.setattr(
        "pyright_profile.discover_pyright",
        lambda *a, **k: _mismatched_pyright_identity(),
    )
    check = doctor_lsp.pyright_check(tmp_path, tmp_path, deadline=time.monotonic() + SHORT_TIMEOUT)
    assert check["status"] == "degraded"
    assert "pyright_version_mismatch" in check["details"]["codes"]


def test_doctor_lsp_check_reports_no_owners_when_absent(tmp_path) -> None:
    from llm_wiki import doctor_lsp

    check = doctor_lsp.lsp_runtime_check(tmp_path, datetime.now(UTC), deadline=float("inf"))
    assert check["status"] == "ok"
    assert check["details"]["owners"] == []


def test_a_vault_that_has_recorded_no_claim_is_healthy(tmp_path) -> None:
    """A new vault has no claim index, and that is not something to repair."""
    from llm_wiki import doctor_deletion

    result = doctor_deletion.claim_check(tmp_path, tmp_path)

    assert result["status"] == "ok"
    assert result["details"]["index"] == "missing"
