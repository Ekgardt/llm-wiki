from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import llm_client
import pytest
from reliable_memory import canonical_json_bytes


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
    descriptor = llm_client.provider_candidates("codex", max_tokens=10)[0]
    monkeypatch.setitem(llm_client._PROBES, "codex", lambda descriptor: False)
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
    descriptor = llm_client.provider_candidates("codex", max_tokens=10)[0]
    probes = []

    def probe(actual_descriptor):
        probes.append(True)
        assert actual_descriptor is descriptor
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
    descriptor = llm_client.provider_candidates("openai", max_tokens=10)[0]
    monkeypatch.setitem(llm_client._PROBES, "openai", lambda descriptor: True)
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
    descriptor = llm_client.provider_candidates("codex", max_tokens=10)[0]
    captured = []
    monkeypatch.setitem(llm_client._PROBES, "codex", lambda descriptor: True)

    def backend(descriptor, prompt, system_prompt, schema):
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


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_real_http_provider_descriptors_are_float_free(provider, monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_MODEL", "resolved-model")

    descriptor = llm_client.provider_candidates(provider, max_tokens=321)[0]

    assert descriptor.model == "resolved-model"
    assert descriptor.inference_settings == {
        "max_tokens": 321,
        "temperature_milli": 200,
    }
    assert not any(isinstance(value, float) for value in descriptor.inference_settings.values())


def test_provider_descriptor_has_restricted_canonical_representation(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_MODEL", "resolved-model")
    descriptor = llm_client.provider_candidates("openai", max_tokens=321)[0]

    canonical = descriptor.canonical()

    assert canonical["provider"] == "openai"
    assert canonical["model"] == "resolved-model"
    assert canonical["inference_settings"]["temperature_milli"] == 200
    assert canonical["inference_settings"]["max_tokens"] == 321
    assert canonical_json_bytes(canonical)
    with pytest.raises(TypeError, match="float"):
        replace(descriptor, inference_settings={"temperature": 0.2}).canonical()


def test_call_candidate_uses_resolved_model_and_settings_after_env_changes(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_MODEL", "resolved-model")
    descriptor = llm_client.provider_candidates("openai", max_tokens=321)[0]
    monkeypatch.setenv("MEMORY_LLM_MODEL", "changed-model")
    captured = []
    monkeypatch.setitem(llm_client._PROBES, "openai", lambda descriptor: True)

    def backend(actual_descriptor, prompt, system_prompt, schema):
        captured.append(actual_descriptor)
        return "answer"

    monkeypatch.setitem(llm_client._BACKENDS, "openai", backend)

    result = llm_client.call_candidate(descriptor, "prompt", "system")

    assert result.text == "answer"
    assert captured == [descriptor]
    assert captured[0].model == "resolved-model"
    assert captured[0].inference_settings["max_tokens"] == 321


def test_http_endpoint_identity_is_normalized_hashed_and_secret_safe(monkeypatch):
    raw = "HTTPS://Example.COM:443/v1/"
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", raw)

    descriptor = llm_client.provider_candidates("openai", max_tokens=321)[0]
    canonical = descriptor.canonical()
    encoded = canonical_json_bytes(canonical).decode("utf-8")

    assert canonical["capabilities"]["endpoint_sha256"] == hashlib.sha256(
        b"https://example.com:443/v1"
    ).hexdigest()
    assert descriptor._endpoint == "https://example.com:443/v1"
    assert raw not in encoded


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:password@example.com/v1",
        "https://example.com/v1?api_key=secret",
        "https://example.com/v1?",
        "https://example.com/v1#fragment",
        "https://example.com/v1#",
    ],
)
def test_http_endpoint_rejects_ambiguous_or_secret_bearing_url(endpoint, monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", endpoint)

    candidate = llm_client.provider_candidates("openai", max_tokens=321)[0]

    assert candidate.resolution_failure == "invalid_configuration"
    result = llm_client.call_candidate(candidate, "prompt", "system", max_tokens=321)
    assert result.failure_class == "invalid_configuration"
    assert result.available is False


def test_healthy_opencode_is_not_blocked_by_invalid_lower_http_candidates(monkeypatch):
    secret = "do-not-log-this-secret"
    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", f"https://example.com/v1?token={secret}")
    monkeypatch.setitem(llm_client._PROBES, "opencode", lambda descriptor: True)
    monkeypatch.setitem(llm_client._BACKENDS, "opencode", lambda *args: "answer")

    assert llm_client.call_llm("prompt") == "answer"


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_forced_invalid_http_provider_returns_none_without_secret_log(
    provider, monkeypatch, capsys
):
    secret = "do-not-log-this-secret"
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", provider)
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", f"https://example.com/v1?token={secret}")

    assert llm_client.call_llm("prompt") is None
    assert secret not in capsys.readouterr().err


def test_invalid_candidate_failure_is_added_to_later_fallback_lineage(monkeypatch):
    original = llm_client._provider_configuration
    seen = []

    def configuration(provider, max_tokens):
        if provider == "codex":
            raise ValueError("invalid codex configuration")
        return original(provider, max_tokens)

    def call(descriptor, *args, **kwargs):
        seen.append(descriptor)
        if descriptor.resolution_failure:
            return llm_client.LLMResult(
                descriptor, None, False, descriptor.resolution_failure, "prompt"
            )
        if descriptor.provider == "opencode":
            return llm_client.LLMResult(descriptor, None, False, "unavailable", "prompt")
        return llm_client.LLMResult(descriptor, "answer", True, None, "prompt")

    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
    monkeypatch.setattr(llm_client, "_provider_configuration", configuration)
    monkeypatch.setattr(llm_client, "call_candidate", call)

    assert llm_client.call_llm("prompt") == "answer"
    assert seen[2].fallback_from == (
        f"{seen[0].identity}:unavailable",
        f"{seen[1].identity}:invalid_configuration",
    )


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_cli_descriptor_records_unenforced_backend_token_default(provider, monkeypatch):
    monkeypatch.setenv("MEMORY_CODEX_MODEL", "gpt-5")
    monkeypatch.setenv("MEMORY_CLAUDE_MODEL", "sonnet")

    first = llm_client.provider_candidates(provider, max_tokens=100)[0]
    second = llm_client.provider_candidates(provider, max_tokens=999)[0]

    assert first.inference_settings["max_tokens"] == "backend_default"
    assert first.capabilities["max_tokens_enforced"] is False
    assert first.canonical() == second.canonical()


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_cli_call_allows_requested_tokens_but_result_keeps_actual_descriptor(
    provider, monkeypatch
):
    descriptor = llm_client.provider_candidates(provider, max_tokens=321)[0]
    monkeypatch.setitem(llm_client._PROBES, provider, lambda descriptor: True)
    monkeypatch.setitem(llm_client._BACKENDS, provider, lambda *args: "answer")

    result = llm_client.call_candidate(
        descriptor,
        "prompt",
        "system",
        max_tokens=999,
    )

    assert result.text == "answer"
    assert result.descriptor is descriptor
    assert result.descriptor.inference_settings["max_tokens"] == "backend_default"


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_http_backend_uses_captured_endpoint_after_env_drift(provider, monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", "https://first.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    descriptor = llm_client.provider_candidates(provider, max_tokens=321)[0]
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", "https://second.example/v1")
    monkeypatch.setitem(llm_client._PROBES, provider, lambda descriptor: True)
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "answer"}}]}
            ).encode("utf-8")

    def urlopen(request, timeout):
        requests.append(request)
        return Response()

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", urlopen)

    result = llm_client.call_candidate(descriptor, "prompt", "system")

    assert result.text == "answer"
    assert requests[0].full_url == "https://first.example:443/v1/chat/completions"


def test_ollama_probe_uses_captured_remote_endpoint_after_env_drift(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", "http://remote.example:22123/v1")
    descriptor = llm_client.provider_candidates("ollama", max_tokens=321)[0]
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", "http://localhost:11434/v1")
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", urlopen)

    assert llm_client.probe_candidate(descriptor) is True
    assert requests[0][0].full_url == "http://remote.example:22123/api/tags"
    assert requests[0][1] == 1.0


def test_call_candidate_rejects_max_tokens_different_from_descriptor(monkeypatch):
    descriptor = llm_client.provider_candidates("openai", max_tokens=321)[0]

    with pytest.raises(ValueError, match="max_tokens"):
        llm_client.call_candidate(descriptor, "prompt", "system", max_tokens=999)


def test_empty_fake_response_preserves_soft_failure(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
    monkeypatch.setenv("MEMORY_LLM_FAKE_RESPONSE", "")

    assert llm_client.call_llm("prompt") == ""


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
