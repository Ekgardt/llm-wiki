"""A provider that died did not answer with nothing.

`_call_claude` ran the CLI with `check=False` and returned `result.stdout or
""`, throwing away `returncode` and `stderr`. A crashed CLI, a CLI that
refused the request, and a CLI that genuinely answered with nothing all
arrived at `_outcome_of` as the same empty string, and all three were reported
as `empty_response` — "the provider answered, and the answer was empty".

In the first LongMemEval run of 2026-08-27 all 26 failures surfaced as the
single opaque string `provider_no_response`. The real cause — the worker
inherited this repository as its working directory, so `claude -p` loaded
`CLAUDE.md` with its ~300 KB of imports and answered as an agent turn — stayed
invisible until someone ran a paired control by hand. Nothing in the run
record could have told them.

This is the same shape as `ProviderTimeout` (2026-08-26): the fix is not to
guess a cause, it is to stop collapsing distinct outcomes into one word.
"""

from __future__ import annotations

import subprocess

import llm_client
import pytest


def _completed(returncode: int, stdout: str, stderr: str):
    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr
        )

    return _run


def _claude_returning(monkeypatch, returncode: int, stdout: str, stderr: str = ""):
    monkeypatch.setattr(llm_client.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(llm_client, "_claude_cli_flags", lambda: frozenset())
    monkeypatch.setattr(
        llm_client.subprocess, "run", _completed(returncode, stdout, stderr)
    )
    return llm_client.provider_candidates("claude", max_tokens=2000)[0]


def test_a_claude_call_that_exits_nonzero_raises_instead_of_answering_nothing(
    monkeypatch,
):
    descriptor = _claude_returning(monkeypatch, 1, "", "Error: not logged in\n")

    with pytest.raises(llm_client.ProviderExited) as caught:
        llm_client._call_claude(descriptor, "prompt", "system")

    assert caught.value.exit_code == 1
    assert "not logged in" in str(caught.value)
    assert "status 1" in str(caught.value)


def test_a_dead_provider_is_not_reported_as_an_empty_answer(monkeypatch):
    def _exited(*args, **kwargs):
        raise llm_client.ProviderExited("fake", 2, "Error: not logged in")

    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]
    monkeypatch.setitem(llm_client._BACKENDS, "fake", _exited)

    result = llm_client.call_candidate(
        descriptor, "prompt", "system", max_tokens=10, available=True
    )

    assert result.failure_class == "provider_exited"
    assert result.failure_class != "empty_response"
    assert result.text is None


def test_the_operator_is_told_the_exit_code_and_what_the_provider_printed(
    monkeypatch, capsys
):
    def _exited(*args, **kwargs):
        raise llm_client.ProviderExited("fake", 127, "claude: command not found")

    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]
    monkeypatch.setitem(llm_client._BACKENDS, "fake", _exited)

    llm_client.call_candidate(
        descriptor, "prompt", "system", max_tokens=10, available=True
    )

    reported = capsys.readouterr().err
    assert "127" in reported
    assert "command not found" in reported


def test_what_the_provider_printed_is_redacted_and_bounded(monkeypatch):
    noise = "x" * 4000
    leaked = f"{noise}\nfatal: token=sk-abcdefghijklmnopqrstuvwxyz012345\n"
    descriptor = _claude_returning(monkeypatch, 1, "", leaked)

    with pytest.raises(llm_client.ProviderExited) as caught:
        llm_client._call_claude(descriptor, "prompt", "system")

    excerpt = caught.value.stderr_excerpt
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in excerpt
    assert "REDACTED" in excerpt
    assert len(excerpt) < len(leaked)
    assert len(excerpt) <= llm_client.PROVIDER_STDERR_EXCERPT_CHARS + 64


def test_a_clean_exit_with_no_output_is_still_an_empty_answer(monkeypatch):
    """The distinction is the point: silence from a healthy CLI stays silence."""
    descriptor = _claude_returning(monkeypatch, 0, "", "")

    assert llm_client._call_claude(descriptor, "prompt", "system") == ""


def test_a_nonzero_exit_that_still_produced_an_answer_keeps_the_answer(monkeypatch):
    """No product behaviour is withdrawn: a usable answer is still returned."""
    descriptor = _claude_returning(monkeypatch, 1, "the answer", "warning: noisy")

    assert llm_client._call_claude(descriptor, "prompt", "system") == "the answer"
