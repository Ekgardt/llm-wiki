"""The Codex final text and its completed-turn counters survive the common boundary."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import llm_client
import pytest
from context_budget import TokenCount, TokenUsage

from tests.test_codex_counts_the_prepared_invocation import (
    codex_executable_alias as codex_executable_alias,
)

pytestmark = pytest.mark.usefixtures("codex_executable_alias")


def _cli_response(monkeypatch, events):
    seen = []

    def run(command, **_kwargs):
        seen.append(command)
        Path(command[command.index("--output-last-message") + 1]).write_text("grounded answer", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="\n".join(json.dumps(event) for event in events).encode(), stderr=b"")

    monkeypatch.setattr(llm_client, "_run_cli", run)
    monkeypatch.setattr(llm_client, "_find_codex_binary", lambda: "codex")
    monkeypatch.setitem(llm_client._PROBES, "codex", lambda _descriptor: True)
    return seen


def _completed(usage):
    return {"type": "turn.completed", "usage": usage}


def test_the_common_result_uses_reported_tokens_without_adding_cached_input_twice(monkeypatch):
    seen = _cli_response(monkeypatch, [
        {"type": "thread.started", "thread_id": "owned-evaluation"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "not the final artifact"}},
        _completed({"input_tokens": 24763, "cached_input_tokens": 24448, "output_tokens": 122}),
    ])
    descriptor = llm_client.provider_candidates("codex", max_tokens=2000)[0]
    result = llm_client.call_candidate(descriptor, "Answer from supplied evidence", "Use only supplied evidence", max_tokens=2000)
    assert result.text == "grounded answer" and result.failure_class is None
    assert result.usage == TokenUsage(input_tokens=24763, output_tokens=122, cache_read_tokens=24448)
    assert result.input_token_count == TokenCount(24763, "reported")
    assert len(seen) == 1 and "--json" in seen[0]
    assert "features.hooks=false" in seen[0]
    assert seen[0][seen[0].index("--sandbox") + 1] == "read-only"


@pytest.mark.parametrize("events", [[], [{"type": "turn.started"}], [_completed(None)]])
def test_an_absent_report_is_unknown_and_does_not_break_a_valid_final_text(monkeypatch, events):
    _cli_response(monkeypatch, events)
    descriptor = llm_client.provider_candidates("codex")[0]
    response = llm_client._call_codex(descriptor, "question", "system")
    assert response.text == "grounded answer"
    assert response.usage == TokenUsage()


@pytest.mark.parametrize("invalid", [True, -1, "123", 1.5])
def test_invalid_reported_counts_do_not_become_zero_or_estimates(monkeypatch, invalid):
    _cli_response(monkeypatch, [_completed({"input_tokens": invalid, "output_tokens": 12})])
    response = llm_client._call_codex(llm_client.provider_candidates("codex")[0], "question", "system")
    assert response.usage.input_tokens is None
    assert response.usage.output_tokens == 12


def test_every_completed_turn_is_included_and_unknown_components_stay_unknown(monkeypatch):
    _cli_response(monkeypatch, [
        _completed({"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3}),
        _completed({"input_tokens": 20, "output_tokens": 4}),
    ])
    response = llm_client._call_codex(llm_client.provider_candidates("codex")[0], "question", "system")
    assert response.usage == TokenUsage(input_tokens=30, output_tokens=7)


def test_usage_never_bypasses_a_cli_failure(monkeypatch):
    def failed(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, stdout=json.dumps(_completed({"input_tokens": 12})).encode(), stderr=b"failed provider")

    monkeypatch.setattr(llm_client, "_run_cli", failed)
    monkeypatch.setattr(llm_client, "_find_codex_binary", lambda: "codex")
    with pytest.raises(RuntimeError):
        llm_client._call_codex(llm_client.provider_candidates("codex")[0], "question", "system")


def test_reported_usage_does_not_bypass_output_dlp(monkeypatch, tmp_path):
    from tests.test_llm_descriptors import _write_dlp_policy

    policy = tmp_path / "policy.json"
    _write_dlp_policy(policy, literals=("grounded answer",))
    monkeypatch.setenv("LLM_WIKI_DLP_POLICY", str(policy))
    _cli_response(monkeypatch, [_completed({"input_tokens": 24, "output_tokens": 12})])
    descriptor = llm_client.provider_candidates("codex", max_tokens=2000)[0]
    result = llm_client.call_candidate(descriptor, "question", "system", max_tokens=2000)
    assert result.text is None and result.failure_class == "dlp_output_blocked"
    assert result.usage == TokenUsage(input_tokens=24, output_tokens=12)
