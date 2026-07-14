from __future__ import annotations

from dataclasses import replace

import llm_client


def test_candidate_zero_has_known_identity_and_no_fallback(monkeypatch):
    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("MEMORY_CODEX_MODEL", "gpt-5")

    candidates = llm_client.provider_candidates()

    assert candidates[0].candidate_index == 0
    assert candidates[0].fallback_from == ()
    assert candidates[1].provider == "codex"
    assert candidates[1].model == "gpt-5"
    assert "structured_output" in candidates[1].capabilities


def test_next_candidate_records_ordered_identity_and_failure_before_call(monkeypatch):
    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("MEMORY_CODEX_MODEL", "gpt-5")
    candidates = llm_client.provider_candidates()
    first = candidates[0]
    lineage = (f"{first.identity}:unavailable",)

    second = replace(candidates[1], fallback_from=lineage)

    assert second.fallback_from == (f"{first.identity}:unavailable",)


def test_call_candidate_reports_unavailable_without_calling_backend(monkeypatch):
    descriptor = llm_client.provider_candidates("codex")[0]
    monkeypatch.setitem(llm_client._PROBES, "codex", lambda: False)
    monkeypatch.setitem(
        llm_client._BACKENDS,
        "codex",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("called")),
    )

    result = llm_client.call_candidate(descriptor, "prompt", "system", max_tokens=10)

    assert result.descriptor is descriptor
    assert result.available is False
    assert result.text is None
    assert result.failure_class == "unavailable"
    assert result.structured_output == "prompt"


def test_explicit_probe_then_call_does_not_probe_twice(monkeypatch):
    descriptor = llm_client.provider_candidates("codex")[0]
    probes = []

    def probe():
        probes.append(True)
        return True

    monkeypatch.setitem(llm_client._PROBES, "codex", probe)
    monkeypatch.setitem(llm_client._BACKENDS, "codex", lambda *args: "answer")

    available = llm_client.probe_candidate(descriptor)
    result = llm_client.call_candidate(
        descriptor,
        "prompt",
        "system",
        max_tokens=10,
        available=available,
    )

    assert result.text == "answer"
    assert probes == [True]


def test_call_candidate_returns_actual_identity_and_native_structured_mode(monkeypatch):
    descriptor = llm_client.provider_candidates("openai")[0]
    monkeypatch.setitem(llm_client._PROBES, "openai", lambda: True)
    monkeypatch.setitem(llm_client._BACKENDS, "openai", lambda *args, **kwargs: '{"ok":true}')

    result = llm_client.call_candidate(
        descriptor,
        "prompt",
        "system",
        max_tokens=10,
        schema={"type": "object"},
    )

    assert result.descriptor == descriptor
    assert result.available is True
    assert result.text == '{"ok":true}'
    assert result.failure_class is None
    assert result.structured_output == "native"


def test_prompt_structured_mode_passes_schema_instruction_to_legacy_backend(monkeypatch):
    descriptor = llm_client.provider_candidates("codex")[0]
    captured = []
    monkeypatch.setitem(llm_client._PROBES, "codex", lambda: True)

    def backend(prompt, system_prompt, max_tokens):
        captured.append(system_prompt)
        return '{"ok":true}'

    monkeypatch.setitem(llm_client._BACKENDS, "codex", backend)

    result = llm_client.call_candidate(
        descriptor,
        "prompt",
        "system",
        max_tokens=10,
        schema={"type": "object"},
    )

    assert result.structured_output == "prompt"
    assert '"type":"object"' in captured[0]


def test_fake_call_llm_delegates_through_candidate_api(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
    seen = []

    def fake_call(descriptor, *args, **kwargs):
        seen.append(descriptor)
        return llm_client.LLMResult(descriptor, "fake answer", True, None, "prompt")

    monkeypatch.setattr(llm_client, "call_candidate", fake_call)

    assert llm_client.call_llm("prompt") == "fake answer"
    assert [descriptor.provider for descriptor in seen] == ["fake"]


def test_call_llm_falls_back_one_candidate_at_a_time_with_lineage(monkeypatch):
    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("MEMORY_CODEX_MODEL", "gpt-5")
    seen = []

    def fake_call(descriptor, prompt, system_prompt, *, max_tokens, schema=None):
        seen.append(descriptor)
        if descriptor.candidate_index == 0:
            return llm_client.LLMResult(
                descriptor=descriptor,
                text=None,
                available=False,
                failure_class="unavailable",
                structured_output="prompt",
            )
        return llm_client.LLMResult(
            descriptor=descriptor,
            text="answer",
            available=True,
            failure_class=None,
            structured_output="prompt",
        )

    monkeypatch.setattr(llm_client, "call_candidate", fake_call)

    assert llm_client.call_llm("prompt") == "answer"
    assert seen[0].fallback_from == ()
    assert seen[1].fallback_from == (f"{seen[0].identity}:unavailable",)


def test_forced_provider_remains_strict(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "codex")
    seen = []

    def unavailable(descriptor, *args, **kwargs):
        seen.append(descriptor.provider)
        return llm_client.LLMResult(
            descriptor, None, False, "unavailable", "prompt"
        )

    monkeypatch.setattr(llm_client, "call_candidate", unavailable)

    assert llm_client.call_llm("prompt") is None
    assert seen == ["codex"]
