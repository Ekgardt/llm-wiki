"""A CLI provider is ended with its whole tree, and one timeout ends the chain.

The leftovers of finding M-A8 of the third audit. See
`docs/research/2026-09-17-a-cli-provider-dies-with-its-children-and-the-chain-costs-one-deadline.md`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import llm_client  # noqa: E402


class _Spawned:
    """A child that never finishes, as a wrapper with a live grandchild behaves."""

    pid = 4242
    returncode = None
    stdout = None
    stderr = None

    def __init__(self, events: list) -> None:
        self.events = events
        self.attempts = 0

    def communicate(self, input=None, timeout=None):  # noqa: A002 - Popen's own name
        self.attempts += 1
        self.events.append(("communicate", timeout))
        if self.attempts == 1:
            raise subprocess.TimeoutExpired(["claude"], timeout)
        self.returncode = -9
        return "", ""

    def kill(self):
        self.events.append(("kill",))


@pytest.fixture
def slow_cli(monkeypatch: pytest.MonkeyPatch) -> list:
    """Every CLI call meets a child that outlives its deadline."""
    import sync_memory

    events: list = []
    monkeypatch.setattr(
        sync_memory.subprocess, "Popen", lambda *a, **k: _Spawned(events)
    )
    monkeypatch.setattr(sync_memory, "_kill_process_tree", lambda process: events.append(("tree", process.pid)))
    monkeypatch.setattr(llm_client.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(llm_client, "_claude_cli_flags", lambda: frozenset())
    return events


def test_a_claude_call_that_runs_out_of_time_ends_the_whole_tree(slow_cli) -> None:
    descriptor = llm_client.provider_candidates("claude", max_tokens=2000)[0]

    with pytest.raises(llm_client.ProviderTimeout):
        llm_client._call_claude(descriptor, "prompt", "system")

    assert ("tree", _Spawned.pid) in slow_cli


def test_a_codex_call_that_runs_out_of_time_ends_the_whole_tree(
    slow_cli, tmp_path: Path
) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_bytes(b"hello")

    with pytest.raises(llm_client.ProviderTimeout):
        llm_client._codex_last_message(
            ["codex"], str(prompt), str(tmp_path / "out.txt")
        )

    assert ("tree", _Spawned.pid) in slow_cli


def test_an_unproven_cleanup_is_named_in_the_timeout(monkeypatch) -> None:
    exc = subprocess.TimeoutExpired(["claude"], 90)
    exc.cleanup_error = "taskkill_failed"

    assert llm_client._cleanup_note(exc) == " (process cleanup unverified: taskkill_failed)"
    assert llm_client._cleanup_note(subprocess.TimeoutExpired(["claude"], 90)) == ""


def test_a_powershell_shim_is_not_offered_as_the_codex_binary(
    monkeypatch, tmp_path: Path
) -> None:
    """CreateProcess cannot start a .ps1, so such an install failed for ever."""
    npm = tmp_path / "npm"
    npm.mkdir()
    (npm / "codex.ps1").write_bytes(b"# shim\n")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    assert llm_client._windows_codex_candidate() is None

    (npm / "codex.cmd").write_bytes(b"@echo off\n")
    assert llm_client._windows_codex_candidate() == str(npm / "codex.cmd")


def test_a_timeout_ends_the_chain_and_a_plain_failure_does_not(monkeypatch) -> None:
    """Every step budget is sized for one deadline, not for one a provider."""
    assert (
        llm_client.chain_stops_after("provider_timeout"),
        llm_client.chain_stops_after("provider_exited"),
        llm_client.chain_stops_after("empty_response"),
        llm_client.chain_stops_after(None),
    ) == (True, False, False, False)


def _timing_out(descriptor, *args, **kwargs):
    return llm_client.LLMResult(
        descriptor, None, True, "provider_timeout", "prompt"
    )


def test_the_chain_does_not_try_the_next_provider_after_a_timeout(
    monkeypatch, capsys
) -> None:
    tried: list[str] = []

    def call(descriptor, *args, **kwargs):
        tried.append(descriptor.provider)
        return _timing_out(descriptor, *args, **kwargs)

    monkeypatch.setattr(llm_client, "forced_provider", lambda: "")
    monkeypatch.setattr(llm_client, "call_candidate", call)

    result = llm_client.call_llm_result("a question", "be brief")

    assert (result, len(tried)) == (None, 1)
    assert "the remaining providers were not tried" in capsys.readouterr().err
