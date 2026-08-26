"""A provider that ran out of time did not answer with nothing.

The nightly pass of 2026-08-26 failed its compile step and left this line in
`logs/nightly-2026-08-26.md`:

    compile: FAILED - RuntimeError: no LLM provider produced a validated
    compile plan: ...; draft:claude:<implicit>:empty_response; ...

`empty_response` means "the provider answered, and the answer was empty". The
same daily log compiled cleanly nine hours later from the same vault, so that
was not what happened. `_call_claude` turned a `TimeoutExpired` into `""`, and
`_outcome_of` calls every empty string `empty_response`. The operator reading
that log could not tell a silent provider from a slow one.
"""

from __future__ import annotations

import subprocess

import llm_client
import pytest


def _raise_timeout(*args, **kwargs):
    raise subprocess.TimeoutExpired(cmd="claude", timeout=90)


def test_a_claude_call_that_runs_out_of_time_raises_instead_of_answering_nothing(
    monkeypatch,
):
    monkeypatch.setattr(llm_client.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(llm_client, "_claude_cli_flags", lambda: frozenset())
    monkeypatch.setattr(llm_client.subprocess, "run", _raise_timeout)
    descriptor = llm_client.provider_candidates("claude", max_tokens=2000)[0]

    with pytest.raises(llm_client.ProviderTimeout):
        llm_client._call_claude(descriptor, "prompt", "system")


def test_a_timed_out_provider_is_not_reported_as_an_empty_answer(monkeypatch):
    def _timed_out(*args, **kwargs):
        raise llm_client.ProviderTimeout("claude did not answer within 90s")

    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]
    monkeypatch.setitem(llm_client._BACKENDS, "fake", _timed_out)

    result = llm_client.call_candidate(
        descriptor, "prompt", "system", max_tokens=10, available=True
    )

    assert result.failure_class == "provider_timeout"
    assert result.text is None
    assert result.available is True


def test_the_lineage_names_the_timeout_so_a_log_reader_can_tell_them_apart(
    monkeypatch,
):
    def _timed_out(*args, **kwargs):
        raise llm_client.ProviderTimeout("claude did not answer within 90s")

    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]
    monkeypatch.setitem(llm_client._BACKENDS, "fake", _timed_out)

    result = llm_client.call_candidate(
        descriptor, "prompt", "system", max_tokens=10, available=True
    )

    item = llm_client._llm_fallback_item(result)
    assert item.endswith(":provider_timeout")
    assert "empty_response" not in item
