"""The Windows dependency audit accepts advisories only on the fork without a fix.

pip-audit judges each exported requirement's marker against the interpreter it runs
on and silently skips the rest. The platform's dependencies check reads every
package of `uv.lock` on Linux; the remaining GitHub leg audits what a Windows
interpreter installs, and its accepted advisories are bound to the one fork that
has no fix.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
UNFIXED_FORK_PYTHON = "3.10"


def _audit_job() -> dict:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    )
    return workflow["jobs"]["dependency-audit"]


def _legs() -> list[dict]:
    return _audit_job()["strategy"]["matrix"]["include"]


def _audit_commands() -> str:
    return "\n".join(str(step.get("run", "")) for step in _audit_job()["steps"])


def _names_advisories_only(leg: dict) -> bool:
    return re.fullmatch(r"(GHSA(-[0-9a-z]{4}){3} ?)+", leg["accepted"].strip()) is not None


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
