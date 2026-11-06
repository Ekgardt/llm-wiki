"""The dependency audit reads every version the lock can install, on some interpreter.

pip-audit judges each exported requirement's marker against the interpreter it runs
on and silently skips the rest. When filelock's fixed releases dropped Python 3.10,
the lock forked it by Python version, and an audit run on Python 3.12 passed while
the 3.10 pin carried known advisories. These tests walk `uv.lock` the way an install
does, once per audit leg, and require the legs together to install every locked
version; the accepted advisories are bound to the one fork that has no fix.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml
from packaging.markers import Marker

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 only
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "llm-wiki"
UNFIXED_FORK_PYTHON = "3.10"
RUNNER_PLATFORMS = {
    "ubuntu-24.04": {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix", "platform_machine": "x86_64"},
    "windows-2025": {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt", "platform_machine": "AMD64"},
    "macos-15": {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix", "platform_machine": "arm64"},
}


def _audit_job() -> dict:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))
    return workflow["jobs"]["dependency-audit"]


def _legs() -> list[dict]:
    return _audit_job()["strategy"]["matrix"]["include"]


def _lock() -> dict:
    with (ROOT / "uv.lock").open("rb") as stream:
        return tomllib.load(stream)


def _environment(leg: dict) -> dict[str, str]:
    version = str(leg["python"])
    return {
        **RUNNER_PLATFORMS[leg["os"]],
        "python_version": version,
        "python_full_version": f"{version}.0",
        "implementation_name": "cpython",
        "implementation_version": f"{version}.0",
        "platform_python_implementation": "CPython",
        "platform_release": "",
        "platform_version": "",
        "extra": "",
    }


def _by_name(lock: dict) -> dict[str, list[dict]]:
    packages: dict[str, list[dict]] = {}
    for package in lock["package"]:
        packages.setdefault(package["name"], []).append(package)
    return packages


def _resolve(edge: dict, packages: dict[str, list[dict]]) -> dict:
    candidates = packages[edge["name"]]
    if "version" in edge:
        return next(package for package in candidates if package["version"] == edge["version"])
    assert len(candidates) == 1, edge
    return candidates[0]


def _edges(package: dict, extras: list[str]) -> list[dict]:
    optional = package.get("optional-dependencies", {})
    return package.get("dependencies", []) + [edge for extra in extras for edge in optional.get(extra, [])]


def _every_root_edge(root: dict) -> list[dict]:
    groups = list(root.get("optional-dependencies", {}).values()) + list(root.get("dev-dependencies", {}).values())
    return root.get("dependencies", []) + [edge for group in groups for edge in group]


def _applies(edge: dict, environment: dict[str, str]) -> bool:
    return "marker" not in edge or Marker(edge["marker"]).evaluate(environment)


def _applicable(edges: list[dict], environment: dict[str, str]) -> list[dict]:
    return [edge for edge in edges if _applies(edge, environment)]


def _visit(edge: dict, packages: dict[str, list[dict]], environment: dict[str, str], seen: set) -> list[dict]:
    """Marks the package `edge` installs as seen and returns its new edges, if any."""
    package = _resolve(edge, packages)
    key = (package["name"], package["version"], tuple(edge.get("extra", [])))
    if key in seen:
        return []
    seen.add(key)
    return _applicable(_edges(package, edge.get("extra", [])), environment)


def _installed(lock: dict, environment: dict[str, str]) -> set[tuple[str, str]]:
    """Every (name, version) an install with all extras and groups gets in `environment`."""
    packages = _by_name(lock)
    pending = _applicable(_every_root_edge(packages[PROJECT][0]), environment)
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    while pending:
        pending.extend(_visit(pending.pop(), packages, environment, seen))
    return {(name, version) for name, version, _ in seen if name != PROJECT}


def _audit_commands() -> str:
    return "\n".join(str(step.get("run", "")) for step in _audit_job()["steps"])


def _names_advisories_only(leg: dict) -> bool:
    return re.fullmatch(r"(GHSA(-[0-9a-z]{4}){3} ?)+", leg["accepted"].strip()) is not None


def test_the_audit_legs_together_install_every_locked_version() -> None:
    lock = _lock()
    locked = {(package["name"], package["version"]) for package in lock["package"] if package["name"] != PROJECT}
    audited = set().union(*(_installed(lock, _environment(leg)) for leg in _legs()))

    assert sorted(locked - audited) == []


def test_the_walk_sees_the_fork_a_single_interpreter_misses() -> None:
    """Guards the walk itself: one Python 3.12 leg must not reach the 3.10 filelock pin."""
    lock = _lock()
    only_312 = _installed(lock, _environment({"os": "ubuntu-24.04", "python": "3.12"}))
    only_310 = _installed(lock, _environment({"os": "ubuntu-24.04", "python": "3.10"}))
    filelock_310 = {pin for pin in only_310 if pin[0] == "filelock"}

    assert filelock_310 and not filelock_310 & only_312


def test_advisories_are_accepted_only_on_the_fork_without_a_fix() -> None:
    """An accepted advisory is a recorded decision, scoped to Python 3.10 and nothing wider.

    It lapses with the support it exists for: once `requires-python` no longer
    admits 3.10, this fails until the acceptance is removed.
    """
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    accepting = [leg for leg in _legs() if leg.get("accepted")]

    assert {str(leg["python"]) for leg in accepting} <= {UNFIXED_FORK_PYTHON}
    assert all(map(_names_advisories_only, accepting))
    assert not accepting or 'requires-python = ">=3.10"' in project
    assert "--ignore-vuln GHSA" not in _audit_commands()


def test_every_leg_audits_its_own_interpreter() -> None:
    assert "uv python pin ${{ matrix.python }}" in _audit_commands()
    assert _audit_job()["runs-on"] == "${{ matrix.os }}"
    assert _audit_job()["strategy"]["fail-fast"] is False
