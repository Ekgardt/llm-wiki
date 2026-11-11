"""Deterministic Git initialization and identity checks for the navigation qualification fixture."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
BENCHMARK_ROOT = Path(__file__).resolve().parent
for _path in (SCRIPTS_ROOT, BENCHMARK_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from generate_python_qualification import (  # noqa: E402
    WORKLOAD_CATALOG_PATH,
    QualificationRepository,
    current_gold_sha256,
    current_source_manifest_sha256,
    workload_catalog_bytes,
)
from navigation_benchmark_deadlines import (  # noqa: E402
    BenchmarkTimeoutError,
    _check_run_deadline,
    _operation_deadline,
)
from repository_scope import sanitized_git_environment  # noqa: E402


class FixtureIdentityError(RuntimeError):
    """Raised when generated source or gold differs from the checked-in pins."""


class QualifiedIdentityError(RuntimeError):
    """Raised when no exact, already-installed Pyright identity is available."""

    def __init__(self, message: str, failed_checks: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.failed_checks = failed_checks


def _git_timeout(deadline: float | None, monotonic: Callable[[], float]) -> float:
    if deadline is None:
        return 30.0
    operation_end = _operation_deadline(deadline, monotonic=monotonic)
    return min(30.0, max(0.001, operation_end - monotonic()))


def _git_timeout_error(deadline: float | None, monotonic: Callable[[], float]) -> Exception:
    if deadline is not None and monotonic() >= deadline:
        return BenchmarkTimeoutError("benchmark run deadline exceeded")
    return RuntimeError("deterministic qualification Git command timed out")


def _run_git(
    command: list[str],
    root: Path,
    environment: dict[str, str],
    timeout: float,
    capture: bool,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise _git_timeout_error(deadline, monotonic) from exc
    if result.returncode != 0:
        raise RuntimeError("deterministic qualification Git command failed")
    if deadline is not None:
        _check_run_deadline(deadline, monotonic=monotonic)
    return result


def _git_stdout(result: subprocess.CompletedProcess, capture: bool) -> str:
    if not capture:
        return ""
    return result.stdout.decode("ascii", errors="strict").strip()


def initialize_deterministic_git(
    repository_root: Path,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Create and commit a deterministic local repository after generation."""
    root = Path(repository_root).resolve(strict=True)
    if not root.is_dir() or (root / ".git").exists():
        raise ValueError("qualification Git root must be a fresh generated directory")
    environment = sanitized_git_environment()
    for name in ("GIT_DEFAULT_HASH", "GIT_TEMPLATE_DIR"):
        environment.pop(name, None)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "LLM Wiki Qualification",
            "GIT_AUTHOR_EMAIL": "qualification@llm-wiki.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_NAME": "LLM Wiki Qualification",
            "GIT_COMMITTER_EMAIL": "qualification@llm-wiki.invalid",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "TZ": "UTC",
        }
    )
    with tempfile.TemporaryDirectory(prefix="code-navigation-git-isolation-") as temporary:
        isolation = Path(temporary)
        template = isolation / "template"
        hooks = isolation / "hooks"
        config = isolation / "config"
        template.mkdir()
        hooks.mkdir()
        config.write_bytes(b"")
        environment.update(
            GIT_CONFIG_GLOBAL=str(config),
            GIT_CONFIG_SYSTEM=str(config),
            GIT_TEMPLATE_DIR=str(template),
        )
        command_prefix = [
            "git",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.safecrlf=false",
            "-c",
            "core.filemode=false",
            "-c",
            f"core.hooksPath={hooks}",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
        ]

        def run(*arguments: str, capture: bool = False) -> str:
            timeout = _git_timeout(deadline, monotonic)
            result = _run_git(
                [*command_prefix, *arguments], root, environment, timeout, capture, deadline, monotonic
            )
            return _git_stdout(result, capture)

        run(
            "init",
            "--quiet",
            "--object-format=sha1",
            "--initial-branch=qualification",
            f"--template={template}",
        )
        run("add", "--all")
        run(
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "--no-verify",
            "-m",
            "qualification fixture",
        )
        commit = run("rev-parse", "--verify", "HEAD^{commit}", capture=True)
        if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            raise RuntimeError("deterministic qualification Git commit is invalid")
        return commit


def _require_source_hash(repository: QualificationRepository, manifest: Mapping[str, object]) -> None:
    current = current_source_manifest_sha256(repository.root)
    if current != repository.source_manifest_sha256 or current != manifest.get("expected_source_manifest_sha256"):
        raise FixtureIdentityError("generated source manifest hash does not match manifest")


def _require_gold_hash(repository: QualificationRepository, manifest: Mapping[str, object]) -> None:
    current = current_gold_sha256(repository)
    if current != repository.gold_sha256 or current != manifest.get("expected_gold_sha256"):
        raise FixtureIdentityError("generated gold hash does not match manifest")


def verify_repository_identity(
    repository: QualificationRepository,
    manifest: Mapping[str, object],
) -> None:
    if repository.line_count != manifest.get("fixture_lines"):
        raise FixtureIdentityError("generated fixture line count does not match manifest")
    _require_source_hash(repository, manifest)
    catalog = (repository.root / WORKLOAD_CATALOG_PATH).read_bytes()
    if catalog != workload_catalog_bytes(repository.workloads):
        raise FixtureIdentityError("generated workload catalog does not match workloads")
    _require_gold_hash(repository, manifest)


def _qualified_identity_checks(identity: object, manifest: Mapping[str, object]) -> dict[str, bool]:
    """Fixed public check names; never serialize paths or arbitrary identity values."""
    return {
        "status": getattr(identity, "status", None) == "qualified",
        "qualified": getattr(identity, "qualified", None) is True,
        "pyright_version": getattr(identity, "version", None) == manifest["pyright_version"],
        "package_sha256": getattr(identity, "package_sha256", None) == manifest["pyright_package_sha256"],
        "node_major": getattr(identity, "node_major", None) == manifest["node_major"],
        "node_version": isinstance(getattr(identity, "node_version", None), str)
        and bool(getattr(identity, "node_version", None)),
        "degradation_codes": not tuple(getattr(identity, "degradation_codes", ())),
    }


def _require_qualified_identity(identity: object, manifest: Mapping[str, object]) -> None:
    failed = tuple(name for name, passed in _qualified_identity_checks(identity, manifest).items() if not passed)
    if failed:
        raise QualifiedIdentityError(
            "an exact qualified Pyright package and Node identity is required", failed
        )
