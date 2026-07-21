"""Shared fixtures and helpers for code-kernel tests."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from corpus_snapshot import CapturedSource, CorpusSnapshot
from reliable_memory import canonical_json_bytes, validate_state_root
from repository_scope import sanitized_git_environment

FIXTURE_ROOT = Path(__file__).parent / "fixtures/code_kernel/python"


def create_python_repository(destination: Path) -> Path:
    shutil.copytree(FIXTURE_ROOT, destination)
    environment = sanitized_git_environment()
    subprocess.run(
        ["git", "init"],
        cwd=destination,
        env=environment,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
    )
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.test"],
        cwd=destination,
        env=environment,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
    )
    subprocess.run(
        ["git", "config", "user.name", "Code Kernel Fixture"],
        cwd=destination,
        env=environment,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=destination,
        env=environment,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
    )
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=destination,
        env=environment,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
    )
    return destination


def fixture_digest(root: Path) -> str:
    values = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    }
    return hashlib.sha256(canonical_json_bytes(values)).hexdigest()


def source_bytes(snapshot: CorpusSnapshot, source_id: str) -> bytes:
    matches = [
        source.content
        for source in snapshot.sources
        if source.record.logical_id == source_id
    ]
    if len(matches) != 1:
        raise KeyError(source_id)
    return matches[0]


def source_by_path(snapshot: CorpusSnapshot, relative_path: str) -> CapturedSource:
    matches = [
        source
        for source in snapshot.sources
        if source.record.relative_path == relative_path
    ]
    if len(matches) != 1:
        raise KeyError(relative_path)
    return matches[0]


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    validate_state_root(root)
    return root


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return create_python_repository(tmp_path / "repository")
