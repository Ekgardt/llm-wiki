"""Fixtures every test may ask for by name: a state root, a Python repository, a catalog.

The root conftest loads only this plugin, so a test pays for these three and not for
the code-kernel helpers they used to live beside.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from reliable_memory import validate_state_root
from repository_scope import sanitized_git_environment

FIXTURE_ROOT = Path(__file__).parent / "fixtures/code_kernel/python"


def copy_python_fixture(destination: Path) -> Path:
    shutil.copytree(
        FIXTURE_ROOT, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    return destination


def create_python_repository(destination: Path) -> Path:
    copy_python_fixture(destination)
    environment = sanitized_git_environment()
    for name in tuple(environment):
        if name in {"GIT_DEFAULT_HASH", "GIT_TEMPLATE_DIR"} or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            environment.pop(name)
    isolation = destination / ".git-test-isolation"
    hooks = isolation / "hooks"
    template = isolation / "template"
    hooks.mkdir(parents=True)
    template.mkdir(parents=True)
    environment.update(
        GIT_AUTHOR_DATE="@946684800 +0000",
        GIT_AUTHOR_EMAIL="fixture@example.test",
        GIT_AUTHOR_NAME="Code Kernel Fixture",
        GIT_COMMITTER_DATE="@946684800 +0000",
        GIT_COMMITTER_EMAIL="fixture@example.test",
        GIT_COMMITTER_NAME="Code Kernel Fixture",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_TEMPLATE_DIR=str(template.resolve()),
        GIT_TERMINAL_PROMPT="0",
    )

    def run_git(*arguments: str) -> None:
        subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={hooks.resolve()}",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "tag.gpgSign=false",
                *arguments,
            ],
            cwd=destination,
            env=environment,
            check=True,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            # A guard against a hung git, not a performance budget: `git add .`
            # over a fixture tree took longer than ten seconds on a loaded
            # hosted Windows runner and failed the tests that used it.
            timeout=120,
        )

    run_git("init", "--initial-branch=main", f"--template={template.resolve()}")
    run_git("config", "user.email", "fixture@example.test")
    run_git("config", "user.name", "Code Kernel Fixture")
    run_git("add", ".")
    run_git("commit", "-m", "fixture")
    return destination



@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    validate_state_root(root)
    return root


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return create_python_repository(tmp_path / "repository")


@pytest.fixture
def catalog(state_root: Path):
    from llm_wiki.generation_catalog_store import GenerationCatalog

    return GenerationCatalog(state_root)
