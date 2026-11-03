from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def project() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)


def test_production_baseline_owns_mcp_v1_and_stdlib_toml(
    project: dict[str, object],
) -> None:
    dependencies = project["project"]["dependencies"]
    assert "mcp>=1.30,<2" in dependencies
    assert project["project"]["requires-python"] == ">=3.14,<3.15"
    assert not any(re.match(r"tomli(?:[<>=;\[]|$)", requirement) for requirement in dependencies)
    assert project["project"]["optional-dependencies"]["mcp-server"] == []


def test_hybrid_and_dev_profiles_own_their_imports(project: dict[str, object]) -> None:
    optional = project["project"]["optional-dependencies"]
    hybrid = optional["hybrid"]
    code_graph = optional["code-graph"]
    dev = project["dependency-groups"]["dev"]

    assert "numpy>=2.2.6,<3" in hybrid
    for requirement in (
        "jsonschema>=4.26,<5",
        "numpy>=2.2.6,<3",
        "jedi>=0.19,<1",
        "tree-sitter>=0.23,<1",
    ):
        assert requirement in dev
    assert {requirement for requirement in code_graph if requirement.startswith("tree-sitter-")} <= set(dev)


def test_uv_version_contract_is_exact_and_current(project: dict[str, object]) -> None:
    assert project["tool"]["uv"]["required-version"] == "==0.12.3"
