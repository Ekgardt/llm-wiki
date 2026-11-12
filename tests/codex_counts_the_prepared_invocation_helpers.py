"""Helpers shared by test_codex_counts_the_prepared_invocation.py and the test files importing them."""
from __future__ import annotations

import subprocess
import time
from dataclasses import replace
from pathlib import Path

import llm_client
import pytest


def _empty_mcp_discovery(executable, command, neutral, environment, deadline):
    """The mocked CLI's native metadata, retaining executable and deadline guards."""
    llm_client.require_codex_executable(executable)
    assert command[-3:] == ['mcp', 'list', '--json']
    assert Path(neutral).is_dir() and deadline > time.monotonic()
    assert environment['CLAUDE_INVOKED_BY']
    return b'[]'


@pytest.fixture
def codex_executable_alias(monkeypatch, tmp_path):
    """Old mocked CLI names bind a real test file while retaining command assertions."""
    executable = tmp_path / "mocked-cli-file"
    executable.write_bytes(b"owned mocked CLI fixture")
    bind = llm_client.bind_codex_executable

    def binding(path):
        return replace(bind(str(executable)), path=path)

    monkeypatch.setattr(llm_client, "bind_codex_executable", binding)
    monkeypatch.setattr(llm_client, "codex_basis_local_command", _empty_mcp_discovery)


def _service(monkeypatch, tmp_path, model="proof-model"):
    monkeypatch.setenv("MEMORY_CODEX_MODEL", model)
    executable = tmp_path / "codex"
    executable.write_bytes(b"owned executable fixture")
    selected = {"path": str(executable), "calls": 0}
    captured = []

    def locate():
        selected["calls"] += 1
        return selected["path"]

    def run(command, **kwargs):
        captured.append((command, kwargs["stdin"].read().decode("utf-8")))
        Path(command[command.index("--output-last-message") + 1]).write_text("answer")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(llm_client, "find_codex_binary", locate)
    monkeypatch.setattr(llm_client, "run_cli", run)
    monkeypatch.setattr(llm_client, "codex_basis_local_command", _empty_mcp_discovery)
    return executable, selected, captured
