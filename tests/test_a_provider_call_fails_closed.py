"""A provider call fails closed: unknown isolation flags, no call; an unknown provider, none.

See docs/research/2026-09-25-a-provider-call-fails-closed.md.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import doctor
import llm_client
import pytest


@pytest.fixture(autouse=True)
def _fresh_probe():
    llm_client._probed_claude_flags.cache_clear()
    yield
    llm_client._probed_claude_flags.cache_clear()


def _help(monkeypatch, answers: list) -> list:
    asked: list = []

    def run(*_args, **_kwargs):
        asked.append(1)
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(llm_client.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(llm_client.subprocess, "run", run)
    return asked


def test_a_failed_probe_is_not_remembered(monkeypatch) -> None:
    asked = _help(
        monkeypatch,
        [subprocess.TimeoutExpired("claude", 30), SimpleNamespace(returncode=0, stdout="--no-session-persistence")],
    )

    first, second, third = (llm_client._claude_cli_flags() for _ in range(3))

    assert (first, second, third, len(asked)) == (None, frozenset({"--no-session-persistence"}), second, 2)


def test_a_call_whose_isolation_is_unknown_is_not_made(monkeypatch) -> None:
    _help(monkeypatch, [SimpleNamespace(returncode=1, stdout="")])
    monkeypatch.setattr(llm_client, "_run_cli", lambda *a, **k: pytest.fail("the CLI must not be called"))
    descriptor = llm_client.provider_candidates("claude", max_tokens=100)[0]

    assert llm_client._call_claude(descriptor, "private text", "system") == ""


@pytest.mark.parametrize(("setting", "order"), [("", list(llm_client.KNOWN_PROVIDERS)), ("ollama", ["ollama"]), ("ollma", [])])
def test_a_name_that_is_no_provider_yields_none(setting, order) -> None:
    assert llm_client._candidate_order(setting) == order


def test_doctor_names_a_provider_setting_that_is_no_provider(monkeypatch, tmp_path) -> None:
    for name in ("knowledge/notes", "scripts"):
        (tmp_path / name).mkdir(parents=True)
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "ollma")

    result = doctor._environment_check(tmp_path, tmp_path)

    assert (result["status"], "MEMORY_LLM_PROVIDER=ollma" in result["message"]) == ("degraded", True)
