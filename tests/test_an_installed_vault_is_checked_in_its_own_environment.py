"""A CI job that installs checks the vault in the environment the install built (2026-09-26).

docs/research/2026-09-26-an-installed-vault-is-checked-in-its-own-environment.md
"""
from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"
_INSTALLS = ("./install.sh", "./install.ps1")


def _installs(job: dict) -> bool:
    commands = " ".join(str(step.get("run", "")) for step in job.get("steps", []))
    return any(command in commands for command in _INSTALLS)


def _environments(job: dict) -> list[dict]:
    return [job.get("env") or {}, *(step.get("env") or {} for step in job.get("steps", []))]


def _redirects_the_environment(job: dict) -> bool:
    return any("UV_PROJECT_ENVIRONMENT" in env for env in _environments(job))


def test_no_installing_job_points_uv_at_another_environment() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    installing = {name for name, job in jobs.items() if _installs(job)}

    assert (bool(installing), sorted(n for n in installing if _redirects_the_environment(jobs[n]))) == (True, [])
