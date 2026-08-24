from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path

import llm_client
import pytest
from context_budget import TokenCount, TokenUsage
from reliable_memory import canonical_json_bytes


def _write_dlp_policy(
    path: Path,
    *,
    literals: tuple[str, ...] = (),
    allow_fingerprints: tuple[str, ...] = (),
) -> None:
    payload = {
        "version": 1,
        "literals": list(literals),
        "allow_fingerprints": list(allow_fingerprints),
    }
    policy = {
        **payload,
        "sha256": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    }
    path.write_text(json.dumps(policy), encoding="utf-8")


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


def test_call_llm_result_preserves_selected_provider_descriptor(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
    monkeypatch.setitem(llm_client._PROBES, "fake", lambda descriptor: True)
    monkeypatch.setitem(llm_client._BACKENDS, "fake", lambda *args: "answer")

    result = llm_client.call_llm_result("prompt", "system", max_tokens=10)

    assert result is not None
    assert result.text == "answer"
    assert result.descriptor.provider == "fake"
    assert result.descriptor.model == "fake-v1"


def test_call_candidate_redacts_builtins_and_policy_literals_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    policy_path = tmp_path / "dlp-policy.json"
    _write_dlp_policy(policy_path, literals=("internal-codename",))
    monkeypatch.setenv("LLM_WIKI_DLP_POLICY", str(policy_path))
    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]
    captured = []

    def backend(descriptor, prompt, system_prompt, schema):
        captured.append((prompt, system_prompt, schema))
        return "answer"

    monkeypatch.setitem(llm_client._BACKENDS, "fake", backend)

    result = llm_client.call_candidate(
        descriptor,
        "Authorization: Bearer transport-secret internal-codename",
        "system password=system-secret",
        max_tokens=10,
        available=True,
    )

    assert result.text == "answer"
    transported = json.dumps(captured)
    assert "transport-secret" not in transported
    assert "system-secret" not in transported
    assert "internal-codename" not in transported
    assert "[REDACTED" in transported


def test_call_candidate_fails_closed_before_transport_for_invalid_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    policy_path = tmp_path / "dlp-policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "literals": ["protected"],
                "allow_fingerprints": [],
                "sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_WIKI_DLP_POLICY", str(policy_path))
    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]
    calls = []

    def backend(*args):
        calls.append(args)
        return "answer"

    monkeypatch.setitem(llm_client._BACKENDS, "fake", backend)

    result = llm_client.call_candidate(
        descriptor, "prompt", "system", max_tokens=10, available=True
    )

    assert result.text is None
    assert result.failure_class == "dlp_policy_error"
    assert calls == []


def test_call_candidate_blocks_sensitive_provider_output(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LLM_WIKI_DLP_POLICY", raising=False)
    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]
    monkeypatch.setitem(
        llm_client._BACKENDS,
        "fake",
        lambda *args: "Authorization: Bearer provider-output-secret",
    )

    result = llm_client.call_candidate(
        descriptor, "prompt", "system", max_tokens=10, available=True
    )

    assert result.text is None
    assert result.failure_class == "dlp_output_blocked"


def test_call_candidate_allows_only_exact_fingerprinted_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    response = "Authorization: Bearer known-false-positive"
    fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest()
    policy_path = tmp_path / "dlp-policy.json"
    _write_dlp_policy(policy_path, allow_fingerprints=(fingerprint,))
    monkeypatch.setenv("LLM_WIKI_DLP_POLICY", str(policy_path))
    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]
    monkeypatch.setitem(llm_client._BACKENDS, "fake", lambda *args: response)

    allowed = llm_client.call_candidate(
        descriptor, "prompt", "system", max_tokens=10, available=True
    )
    monkeypatch.setitem(llm_client._BACKENDS, "fake", lambda *args: response + " changed")
    blocked = llm_client.call_candidate(
        descriptor, "prompt", "system", max_tokens=10, available=True
    )

    assert allowed.text == response
    assert allowed.failure_class is None
    assert blocked.text is None
    assert blocked.failure_class == "dlp_output_blocked"


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


def test_forced_ollama_local_only_rejects_nonliteral_loopback(monkeypatch):
    monkeypatch.setenv("OLLAMA_NO_CLOUD", "1")
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", "http://remote.example:11434/v1")

    descriptor = llm_client.provider_candidates("ollama", max_tokens=321)[0]

    assert descriptor.resolution_failure == "invalid_configuration"


def test_forced_ollama_local_only_accepts_only_local_model_metadata(monkeypatch):
    monkeypatch.setenv("OLLAMA_NO_CLOUD", "1")
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("MEMORY_LLM_MODEL", "qwen3:0.6b")
    descriptor = llm_client.provider_candidates("ollama", max_tokens=321)[0]

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            assert limit == 1024 * 1024 + 1
            return json.dumps(
                {
                    "models": [
                        {
                            "name": "qwen3:0.6b",
                            "model": "qwen3:0.6b",
                            "modified_at": "2026-08-15T00:00:00Z",
                            "size": 523_000_000,
                            "digest": "a" * 64,
                            "details": {
                                "format": "gguf",
                                "family": "qwen3",
                                "families": ["qwen3"],
                                "parameter_size": "0.6B",
                                "quantization_level": "Q4_K_M",
                            },
                        }
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", lambda *args, **kwargs: Response())

    assert descriptor.capabilities["local_only_status"] == "external_runtime_unverified"
    assert llm_client.probe_candidate(descriptor) is True


def test_call_candidate_cannot_bypass_local_only_remote_model_check(monkeypatch):
    monkeypatch.setenv("OLLAMA_NO_CLOUD", "1")
    monkeypatch.setenv("MEMORY_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("MEMORY_LLM_MODEL", "qwen3:0.6b")
    descriptor = llm_client.provider_candidates("ollama", max_tokens=321)[0]
    backend_calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps(
                {
                    "models": [
                        {
                            "name": "qwen3:0.6b",
                            "model": "qwen3:0.6b",
                            "remote_model": "qwen3:0.6b-cloud",
                            "remote_host": "https://ollama.com",
                            "size": 0,
                            "digest": "",
                            "details": {},
                        }
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    monkeypatch.setitem(
        llm_client._BACKENDS,
        "ollama",
        lambda *args: backend_calls.append(args) or "answer",
    )

    result = llm_client.call_candidate(
        descriptor, "prompt", "system", max_tokens=321, available=True
    )

    assert result.text is None
    assert result.failure_class == "unavailable"
    assert backend_calls == []


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


def test_llm_result_preserves_five_argument_positional_compatibility():
    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]

    result = llm_client.LLMResult(descriptor, "answer", True, None, "prompt")

    assert result.usage == TokenUsage()
    assert result.input_token_count is None


def test_fake_descriptor_forwards_the_requested_output_limit():
    descriptor = llm_client.provider_candidates("fake", max_tokens=37)[0]

    assert descriptor.inference_settings["max_tokens"] == 37
    assert descriptor.capabilities["max_tokens_enforced"] is True


def test_call_candidate_accepts_response_envelope_and_plain_string(monkeypatch):
    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]
    usage = TokenUsage(input_tokens=4, output_tokens=2)
    responses = [llm_client.BackendResponse("first", usage), "second"]
    monkeypatch.setitem(llm_client._BACKENDS, "fake", lambda *args: responses.pop(0))

    first = llm_client.call_candidate(descriptor, "prompt", "system", available=True)
    second = llm_client.call_candidate(descriptor, "prompt", "system", available=True)

    assert first.text == "first"
    assert first.usage == usage
    assert second.text == "second"
    assert second.usage == TokenUsage()
    assert second.input_token_count == TokenCount(
        len(b"system\n\nprompt"), "estimated"
    )


def test_call_candidate_counts_injected_schema_text_with_model_adapter(monkeypatch):
    monkeypatch.setenv("MEMORY_CODEX_MODEL", "known-model")
    descriptor = llm_client.provider_candidates("codex", max_tokens=10)[0]
    counted = []
    monkeypatch.setitem(llm_client._BACKENDS, "codex", lambda *args: "answer")

    def adapter(text):
        counted.append(text)
        return 17

    result = llm_client.call_candidate(
        descriptor,
        "user prompt",
        "system prompt",
        schema={"type": "object"},
        available=True,
        token_adapters={"known-model": adapter},
    )

    assert counted == [
        'system prompt\n\nOutput only JSON matching this schema: {"type":"object"}'
        "\n\nuser prompt"
    ]
    assert counted[0].count('{"type":"object"}') == 1
    assert result.input_token_count == TokenCount(17, "tokenizer")


def test_native_schema_is_counted_once_as_compact_sorted_json(monkeypatch):
    descriptor = llm_client.provider_candidates("openai", max_tokens=10)[0]
    schema = {
        "type": "object",
        "properties": {"body": {"type": "string", "description": "x" * 1000}},
    }
    schema_json = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    counted = []
    monkeypatch.setitem(llm_client._BACKENDS, "openai", lambda *args: "answer")

    def adapter(text):
        counted.append(text)
        return len(text)

    result = llm_client.call_candidate(
        descriptor,
        "user prompt",
        "system prompt",
        schema=schema,
        available=True,
        token_adapters={descriptor.model: adapter},
    )

    assert counted == [f"system prompt\n\n{schema_json}\n\nuser prompt"]
    assert counted[0].count(schema_json) == 1
    assert result.input_token_count == TokenCount(len(counted[0]), "tokenizer")
    assert result.input_token_count.tokens > 1000


def test_unserializable_native_schema_counts_unknown_then_reaches_provider_boundary(
    monkeypatch,
):
    descriptor = llm_client.provider_candidates("openai", max_tokens=10)[0]
    called = []

    def backend(*args):
        called.append(True)
        raise TypeError("schema is not serializable")

    monkeypatch.setitem(llm_client._BACKENDS, "openai", backend)

    result = llm_client.call_candidate(
        descriptor,
        "prompt",
        "system",
        schema={"bad": object()},
        available=True,
    )

    assert called == [True]
    assert result.failure_class == "provider_error"
    assert result.input_token_count == TokenCount(tokens=None, source="unknown")


def test_surrogate_input_count_fails_closed_without_crashing_candidate(monkeypatch):
    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]
    monkeypatch.setitem(llm_client._BACKENDS, "fake", lambda *args: "answer")

    result = llm_client.call_candidate(
        descriptor, "prompt\ud800", "system", available=True
    )

    assert result.text == "answer"
    assert result.input_token_count == TokenCount(tokens=None, source="unknown")


def _http_call(monkeypatch, provider, response_data):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    descriptor = llm_client.provider_candidates(provider, max_tokens=37)[0]
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(response_data).encode("utf-8")

    def urlopen(request, timeout):
        requests.append(request)
        return Response()

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", urlopen)
    result = llm_client.call_candidate(
        descriptor, "prompt", "system", available=True, max_tokens=37
    )
    return result, json.loads(requests[0].data), requests[0].full_url


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_openai_compatible_usage_and_request_limit(provider, monkeypatch):
    result, payload, url = _http_call(
        monkeypatch,
        provider,
        {
            "choices": [{"message": {"content": "answer"}}],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 4,
                "prompt_tokens_details": {"cached_tokens": 3},
            },
        },
    )

    assert result.usage == TokenUsage(
        input_tokens=12, output_tokens=4, cache_read_tokens=3
    )
    assert result.input_token_count == TokenCount(12, "reported")
    assert payload["max_tokens"] == 37
    assert url.endswith("/v1/chat/completions")


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_compatibility_usage_ignores_native_ollama_top_level_fields(provider, monkeypatch):
    result, _, _ = _http_call(
        monkeypatch,
        provider,
        {
            "choices": [{"message": {"content": "answer"}}],
            "prompt_eval_count": 9,
            "eval_count": 5,
            "total_duration": 1_999_999,
        },
    )

    assert result.usage == TokenUsage()
    assert result.input_token_count is not None
    assert result.input_token_count.source == "estimated"


def test_malformed_usage_fields_are_ignored_field_by_field(monkeypatch):
    result, _, _ = _http_call(
        monkeypatch,
        "openai",
        {
            "choices": [{"message": {"content": "answer"}}],
            "usage": {
                "prompt_tokens": True,
                "completion_tokens": -1,
                "prompt_tokens_details": {"cached_tokens": 2},
            },
            "total_duration": "secret payload must not leak",
        },
    )

    assert result.text == "answer"
    assert result.usage == TokenUsage(cache_read_tokens=2)


def test_opencode_aggregate_usage_wins_over_part_usage(monkeypatch):
    descriptor = llm_client.provider_candidates("opencode", max_tokens=37)[0]
    calls = []
    responses = [
        {"id": "session-id"},
        {
            "data": {
                "info": {
                    "tokens": {
                        "input": 10,
                        "output": 4,
                        "reasoning": 8,
                        "cache": {"read": 3, "write": 2},
                    },
                    "cost": 0.5,
                    "time": {"created": -1e308, "completed": 1e308},
                },
                "parts": [
                    {"type": "text", "text": None},
                    {"type": "text", "text": False},
                    {"type": "text", "text": 3},
                    {"type": "text", "text": ["wire"]},
                    {"type": "text", "text": {"wire": "value"}},
                    {"type": "text", "text": "answer", "tokens": {"input": 99}},
                ],
            }
        },
    ]

    class Response:
        status = 200

        def __init__(self, data=None):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self.data).encode("utf-8")

    def urlopen(request, timeout):
        calls.append(request)
        if request.method == "DELETE":
            return Response({})
        return Response(responses.pop(0))

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", urlopen)
    result = llm_client.call_candidate(descriptor, "prompt", "", available=True)

    assert result.text == "answer"
    assert result.usage == TokenUsage(
        input_tokens=10,
        output_tokens=4,
        cache_read_tokens=3,
        cache_write_tokens=2,
        estimated_cost=0.5,
        cost_kind="reported",
    )
    prompt_payload = json.loads(calls[1].data)
    assert "max_tokens" not in prompt_payload


def test_opencode_sums_part_usage_only_when_aggregate_is_absent(monkeypatch):
    data = {
        "parts": [
            {"type": "text", "text": "a", "tokens": {"input": 2, "output": 1}},
            {"type": "metadata", "tokens": {"input": 100, "output": 100}},
            {
                "type": "step-finish",
                "tokens": {
                    "input": 3,
                    "output": 4,
                    "cache": {"read": 1, "write": 2},
                },
            },
        ]
    }

    assert llm_client._parse_opencode_usage(data) == TokenUsage(
        input_tokens=3,
        output_tokens=4,
        cache_read_tokens=1,
        cache_write_tokens=2,
    )


def test_opencode_aggregate_parses_reported_cost_and_elapsed_milliseconds():
    usage = llm_client._parse_opencode_usage(
        {
            "info": {
                "tokens": {"input": 3, "output": 2},
                "cost": 0.125,
                "time": {"created": 1000.9, "completed": 1004.8},
            }
        }
    )

    assert usage == TokenUsage(
        input_tokens=3,
        output_tokens=2,
        duration_ms=3,
        estimated_cost=0.125,
        cost_kind="reported",
    )


@pytest.mark.parametrize(
    ("cost", "time", "expected_cost", "expected_kind", "expected_duration"),
    [
        (True, {"created": 10, "completed": 15}, None, "unknown", 5),
        (-1, {"created": 10, "completed": 9}, None, "unknown", None),
        (0.5, {"created": "bad", "completed": 15}, 0.5, "reported", None),
        (float("nan"), {"created": 10, "completed": 15}, None, "unknown", 5),
        (10**400, {"created": 10, "completed": 15}, None, "unknown", 5),
        (0.5, {"created": 10**400, "completed": 10**400}, 0.5, "reported", None),
    ],
)
def test_opencode_malformed_cost_and_time_are_independent(
    cost, time, expected_cost, expected_kind, expected_duration
):
    usage = llm_client._parse_opencode_usage(
        {"info": {"tokens": {"input": 3}, "cost": cost, "time": time}}
    )

    assert usage.input_tokens == 3
    assert usage.estimated_cost == expected_cost
    assert usage.cost_kind == expected_kind
    assert usage.duration_ms == expected_duration


def test_opencode_large_positive_finite_elapsed_time_is_floored():
    created = 1e308
    completed = 1.7e308

    usage = llm_client._parse_opencode_usage(
        {
            "info": {
                "tokens": {"input": 3},
                "cost": 0.25,
                "time": {"created": created, "completed": completed},
            }
        }
    )

    assert usage.duration_ms == math.floor(completed - created)
    assert usage.input_tokens == 3
    assert usage.estimated_cost == 0.25


def test_opencode_only_invalid_text_returns_empty_with_reported_usage(monkeypatch):
    descriptor = llm_client.provider_candidates("opencode", max_tokens=10)[0]
    responses = [
        {"id": "session-id"},
        {
            "info": {"tokens": {"input": 7, "output": 2}},
            "parts": [
                {"type": "text", "text": None},
                {"type": "text", "text": True},
                {"type": "text", "text": 3},
                {"type": "text", "text": ["wire"]},
                {"type": "text", "text": {"wire": "value"}},
            ],
        },
    ]

    class Response:
        def __init__(self, data=None):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self.data).encode("utf-8")

    def urlopen(request, timeout):
        if request.method == "DELETE":
            return Response({})
        return Response(responses.pop(0))

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", urlopen)
    result = llm_client.call_candidate(descriptor, "prompt", "", available=True)

    assert result.failure_class == "empty_response"
    assert result.usage == TokenUsage(input_tokens=7, output_tokens=2)
    assert result.input_token_count == TokenCount(7, "reported")


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_cli_text_response_does_not_fabricate_usage(provider, monkeypatch):
    descriptor = llm_client.provider_candidates(provider, max_tokens=37)[0]
    monkeypatch.setitem(llm_client._BACKENDS, provider, lambda *args: "answer")

    result = llm_client.call_candidate(descriptor, "prompt", "system", available=True)

    assert result.usage == TokenUsage()
    assert result.input_token_count == TokenCount(
        len(b"system\n\nprompt"), "estimated"
    )


def test_provider_error_preserves_pre_call_estimate(monkeypatch):
    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]
    monkeypatch.setitem(
        llm_client._BACKENDS,
        "fake",
        lambda *args: (_ for _ in ()).throw(RuntimeError("provider detail")),
    )

    result = llm_client.call_candidate(descriptor, "prompt", "system", available=True)

    assert result.failure_class == "provider_error"
    assert result.usage == TokenUsage()
    assert result.input_token_count == TokenCount(
        len(b"system\n\nprompt"), "estimated"
    )


def test_empty_response_preserves_pre_call_estimate(monkeypatch):
    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]
    monkeypatch.setitem(llm_client._BACKENDS, "fake", lambda *args: "")

    result = llm_client.call_candidate(descriptor, "prompt", "system", available=True)

    assert result.failure_class == "empty_response"
    assert result.usage == TokenUsage()
    assert result.input_token_count == TokenCount(
        len(b"system\n\nprompt"), "estimated"
    )


def test_empty_envelope_preserves_usage_and_reported_input_count(monkeypatch):
    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]
    usage = TokenUsage(input_tokens=9, output_tokens=3)
    monkeypatch.setitem(
        llm_client._BACKENDS,
        "fake",
        lambda *args: llm_client.BackendResponse("", usage),
    )

    result = llm_client.call_candidate(descriptor, "prompt", "system", available=True)

    assert result.failure_class == "empty_response"
    assert result.usage == usage
    assert result.input_token_count == TokenCount(9, "reported")


def test_whitespace_envelope_preserves_partial_usage_and_pre_call_count(monkeypatch):
    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]
    usage = TokenUsage(output_tokens=3, cache_read_tokens=2)
    monkeypatch.setitem(
        llm_client._BACKENDS,
        "fake",
        lambda *args: llm_client.BackendResponse("  \n", usage),
    )

    result = llm_client.call_candidate(descriptor, "prompt", "system", available=True)

    assert result.failure_class == "empty_response"
    assert result.usage == usage
    assert result.input_token_count == TokenCount(len(b"system\n\nprompt"), "estimated")


def test_non_string_envelope_text_fails_safely_without_losing_usage(monkeypatch):
    descriptor = llm_client.provider_candidates("fake", max_tokens=10)[0]
    usage = TokenUsage(input_tokens=5, cache_write_tokens=1)
    monkeypatch.setitem(
        llm_client._BACKENDS,
        "fake",
        lambda *args: llm_client.BackendResponse(None, usage),
    )

    result = llm_client.call_candidate(descriptor, "prompt", "system", available=True)

    assert result.failure_class == "empty_response"
    assert result.usage == usage
    assert result.input_token_count == TokenCount(5, "reported")


def test_the_claude_call_does_not_inherit_the_operator_persona(monkeypatch):
    """A programmatic call must not answer in the user's configured voice.

    Measured on this machine: `claude -p` loads the user's settings, including
    the output style, and treats a `<system>` block in the prompt as text. The
    compile's draft came back three times as a chat reply — "От вас ничего не
    нужно" — instead of the JSON plan the schema asked for.
    """
    monkeypatch.setattr(
        llm_client, "_claude_cli_flags", lambda: frozenset({"--system-prompt", "--setting-sources"})
    )

    command = llm_client._claude_command("/usr/bin/claude", "some-model", "BE A COMPILER")

    assert "--system-prompt" in command
    assert command[command.index("--system-prompt") + 1] == "BE A COMPILER"
    assert "--setting-sources" in command
    assert command[command.index("--setting-sources") + 1] == ""
    # The system text travels once, through the flag rather than the prompt.
    assert llm_client._claude_stdin("BE A COMPILER", "work") == "work"


def test_an_older_claude_cli_still_gets_the_system_text(monkeypatch):
    monkeypatch.setattr(llm_client, "_claude_cli_flags", lambda: frozenset())

    command = llm_client._claude_command("/usr/bin/claude", None, "BE A COMPILER")

    assert "--system-prompt" not in command
    assert "<system>BE A COMPILER</system>" in llm_client._claude_stdin("BE A COMPILER", "work")
