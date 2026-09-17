"""`doctor --repair` unblocks model tasks only when the provider the calls will use is there.

Research: `docs/research/2026-09-17-repair-asks-about-the-provider-the-operator-chose.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

MODEL_WORK = {"llm.compile", "llm.flush", "llm.query"}


@pytest.fixture
def a_claude_binary_on_the_path(tmp_path, monkeypatch):
    """Another provider is installed; only the environment says which one is used."""
    binary = tmp_path / "bin" / "claude"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary.parent))
    monkeypatch.setenv("PATHEXT", "")
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", "http://127.0.0.1:9/v1")


@pytest.mark.skipif(sys.platform == "win32", reason="a shell stub stands in for the binary")
def test_a_forced_provider_that_is_down_leaves_model_work_blocked(a_claude_binary_on_the_path, monkeypatch):
    import doctor

    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "ollama")

    assert doctor._ready_capabilities() == set()


@pytest.mark.skipif(sys.platform == "win32", reason="a shell stub stands in for the binary")
def test_the_forced_provider_being_there_unblocks_model_work(a_claude_binary_on_the_path, monkeypatch):
    import doctor

    monkeypatch.setenv("MEMORY_LLM_PROVIDER", " Claude ")

    assert doctor._ready_capabilities() == MODEL_WORK
