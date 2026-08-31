"""A provider call must not start inside the vault.

Measured 2026-08-28, paired, on a trivial prompt: 62.15s and 64.60s with the
vault as the working directory against 27.24s and 33.90s from `/tmp` — about
33 seconds of fixed overhead against a 90s ceiling, which is exactly the
`draft:claude:provider_timeout` the live compile was failing with at 16:03:47.
A CLI that discovers project memory from its working directory upwards loads
this repository's `CLAUDE.md`, with the index and log it imports, before it
sees the prompt. Research:
`docs/research/2026-08-28-where-the-provider-runs.md`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

VAULT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VAULT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import llm_client  # noqa: E402


class _Recorder:
    def __init__(self) -> None:
        self.cwds: list[object] = []

    def __call__(self, *args, **kwargs):
        self.cwds.append(kwargs.get("cwd"))
        return subprocess.CompletedProcess(args=args or ("x",), returncode=0, stdout="", stderr="")


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    calls = _Recorder()
    monkeypatch.setattr(llm_client.subprocess, "run", calls)
    return calls


def _descriptor() -> llm_client.ProviderDescriptor:
    return llm_client.ProviderDescriptor(
        provider="claude",
        model=None,
        capabilities=MappingProxyType({}),
        inference_settings=MappingProxyType({}),
        candidate_index=0,
        fallback_from=(),
    )


def _is_outside_the_vault(cwd: object) -> bool:
    if not isinstance(cwd, str):
        return False
    return VAULT not in Path(cwd).resolve().parents and Path(cwd).resolve() != VAULT


def test_the_neutral_directory_is_empty_and_outside_the_vault() -> None:
    with llm_client.provider_cwd() as neutral:
        assert _is_outside_the_vault(neutral)
        assert list(Path(neutral).iterdir()) == []


def test_the_neutral_directory_is_removed_after_the_call() -> None:
    with llm_client.provider_cwd() as neutral:
        path = Path(neutral)
    assert not path.exists()


def test_the_claude_call_runs_outside_the_vault(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_client.shutil, "which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(llm_client, "_claude_command", lambda *_a: ["claude"])
    monkeypatch.setattr(llm_client, "_claude_answer", lambda *_a: "")
    llm_client._claude_cli_flags.cache_clear()
    llm_client._call_claude(_descriptor(), "prompt", "system")
    llm_client._claude_cli_flags.cache_clear()
    outside = [_is_outside_the_vault(cwd) for cwd in recorder.cwds]
    assert outside and all(outside), recorder.cwds


def test_the_codex_call_runs_outside_the_vault(
    recorder: _Recorder, tmp_path: Path
) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("hello", encoding="utf-8")
    llm_client._codex_last_message(["codex"], str(prompt), str(tmp_path / "out.txt"))
    assert [_is_outside_the_vault(cwd) for cwd in recorder.cwds] == [True]
