"""Universal LLM client for memory scripts.

Provides a single `call_llm()` function that auto-detects the best
available LLM backend on this machine. Zero configuration required.

Backend priority (auto-detected, first alive wins):
  1. OpenCode server (HTTP API on localhost:4096)  ← new
  2. Codex CLI (`codex exec`)                       ← was default
  3. Claude CLI (`claude -p`)                       ← new
  4. OpenAI-compatible API (if OPENAI_API_KEY)
  5. Ollama HTTP API (if localhost:11434 alive)

If NONE available: returns None. Callers handle this gracefully (compile
skips, flush treats as FLUSH_OK, query returns error string). The queue
(``scripts/memory_queue.py``) is available as an explicit API for callers
that want deferred execution — ``memory_queue.enqueue()``.

Override backend via MEMORY_LLM_PROVIDER env var:
    MEMORY_LLM_PROVIDER=opencode  (default — uses OpenCode HTTP API)
    MEMORY_LLM_PROVIDER=codex     (uses codex exec)
    MEMORY_LLM_PROVIDER=claude    (uses claude CLI)
    MEMORY_LLM_PROVIDER=openai    (uses OPENAI_API_KEY)
    MEMORY_LLM_PROVIDER=ollama    (uses local Ollama server)
    MEMORY_LLM_PROVIDER=fake      (tests/e2e — returns MEMORY_LLM_FAKE_RESPONSE)

Design:
- NEVER crash the caller: on any LLM failure, return "" (empty string).
- On no-backend-available: enqueue the call as a deferred task.
- Bounded timeouts: 90s per HTTP call. The OpenCode backend makes up to
  three sequential calls (session create, system inject, prompt), so its
  aggregate wall time may reach ~270s; all other backends are single-call.
- Each backend does its own liveness probe — fall-through to next is
  automatic if a backend is installed but not currently running.
"""
from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple

from context_budget import TokenCount, TokenCounter, TokenUsage, count_tokens
from model_dlp import (
    DLPContentBlocked,
    DLPPolicyError,
    load_policy,
    redact_for_transport,
    redact_transport_value,
    require_safe_model_output,
)
from reliable_memory import canonical_json_bytes

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderDescriptor:
    """Resolved identity and behavior of one provider attempt."""

    provider: str
    model: str | None
    capabilities: Mapping[str, object]
    inference_settings: Mapping[str, object]
    candidate_index: int
    fallback_from: tuple[str, ...]
    _endpoint: str | None = field(default=None, repr=False, compare=False)
    _resolution_failure: str | None = field(default=None, repr=False, compare=False)

    @property
    def identity(self) -> str:
        return f"{self.provider}:{self.model or '<implicit>'}"

    @property
    def resolution_failure(self) -> str | None:
        return self._resolution_failure

    def canonical(self) -> dict[str, object]:
        """Return the descriptor in the restricted JSON value domain."""
        value = {
            "provider": self.provider,
            "model": self.model,
            "capabilities": dict(self.capabilities),
            "inference_settings": dict(self.inference_settings),
            "candidate_index": self.candidate_index,
            "fallback_from": list(self.fallback_from),
        }
        return json.loads(canonical_json_bytes(value))


@dataclass(frozen=True)
class LLMResult:
    """Outcome of exactly one provider candidate call."""

    descriptor: ProviderDescriptor
    text: str | None
    available: bool
    failure_class: str | None
    structured_output: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    input_token_count: TokenCount | None = None


@dataclass(frozen=True)
class BackendResponse:
    """Internal response carrying provider text and reported usage."""

    text: str
    usage: TokenUsage = field(default_factory=TokenUsage)


def _wants_local_only(provider: str, forced: str) -> bool:
    if provider != "ollama" or forced != "ollama":
        return False
    return os.environ.get("OLLAMA_NO_CLOUD") == "1"


def _local_only_capabilities(
    capabilities: Mapping[str, object], endpoint: str | None
) -> dict[str, object]:
    if endpoint is None or not _is_literal_loopback_endpoint(endpoint):
        raise ValueError("local-only Ollama requires a literal loopback endpoint")
    updated = dict(capabilities)
    updated["local_only_enforced"] = True
    updated["local_only_status"] = "external_runtime_unverified"
    return updated


def _resolved_descriptor(
    provider: str, index: int, forced: str, max_tokens: int
) -> ProviderDescriptor:
    model, capabilities, settings, endpoint = _provider_configuration(
        provider, max_tokens
    )
    if _wants_local_only(provider, forced):
        capabilities = _local_only_capabilities(capabilities, endpoint)
    return ProviderDescriptor(
        provider=provider,
        model=model,
        capabilities=MappingProxyType(dict(capabilities)),
        inference_settings=MappingProxyType(dict(settings)),
        candidate_index=index,
        fallback_from=(),
        _endpoint=endpoint,
    )


def _unresolved_descriptor(provider: str, index: int) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider=provider,
        model=None,
        capabilities=MappingProxyType({}),
        inference_settings=MappingProxyType({}),
        candidate_index=index,
        fallback_from=(),
        _resolution_failure="invalid_configuration",
    )


def _candidate_descriptor(
    provider: str, index: int, forced: str, max_tokens: int
) -> ProviderDescriptor:
    try:
        return _resolved_descriptor(provider, index, forced, max_tokens)
    except ValueError:
        return _unresolved_descriptor(provider, index)


def provider_candidates(
    forced: str = "",
    *,
    max_tokens: int = 2000,
) -> list[ProviderDescriptor]:
    """Resolve ordered provider identities without probing or calling them."""
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    forced = forced.lower().strip()
    return [
        _candidate_descriptor(provider, index, forced, max_tokens)
        for index, provider in enumerate(_candidate_order(forced))
    ]


def probe_candidate(descriptor: ProviderDescriptor) -> bool:
    """Check one candidate without invoking its model backend."""
    if descriptor.resolution_failure is not None:
        return False
    probe = _PROBES.get(descriptor.provider)
    if probe is None:
        return False
    try:
        return bool(probe(descriptor))
    except Exception:  # noqa: BLE001 - provider probes are an isolation boundary
        return False


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _require_enforced_tokens(effective: object, max_tokens: int | None) -> None:
    if not _is_positive_int(effective):
        raise ValueError("descriptor max_tokens must be a positive integer")
    if max_tokens is not None and max_tokens != effective:
        raise ValueError("max_tokens does not match the resolved provider descriptor")


def _require_backend_default_tokens(effective: object, max_tokens: int | None) -> None:
    if effective != "backend_default":
        raise ValueError("descriptor must record the backend token default")
    if max_tokens is not None and not _is_positive_int(max_tokens):
        raise ValueError("max_tokens request must be a positive integer")


def _require_token_contract(
    descriptor: ProviderDescriptor, max_tokens: int | None
) -> None:
    effective = descriptor.inference_settings.get("max_tokens")
    enforced = descriptor.capabilities.get("max_tokens_enforced")
    if enforced is True:
        _require_enforced_tokens(effective, max_tokens)
        return
    if enforced is False:
        _require_backend_default_tokens(effective, max_tokens)
        return
    raise ValueError("descriptor must declare whether max_tokens is enforced")


def _structured_mode(
    descriptor: ProviderDescriptor, schema: Mapping[str, object] | None
) -> str:
    if schema is None:
        return "prompt"
    if descriptor.capabilities.get("structured_output") == "native":
        return "native"
    return "prompt"


def _prompted_system(
    system_prompt: str, schema: Mapping[str, object] | None, mode: str
) -> str:
    """The schema goes into the system prompt when the backend cannot take it."""
    if schema is None or mode != "prompt":
        return system_prompt
    schema_json = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    instruction = f"Output only JSON matching this schema: {schema_json}"
    if system_prompt:
        return f"{system_prompt}\n\n{instruction}"
    return instruction


def _native_schema_json(schema: Mapping[str, object] | None, mode: str) -> str | None:
    if schema is None or mode != "native":
        return None
    try:
        return json.dumps(schema, sort_keys=True, separators=(",", ":"))
    except Exception:  # noqa: BLE001 - counting must not replace provider validation
        return None


class _Transport(NamedTuple):
    """Call inputs after the DLP boundary has seen them."""

    system_prompt: str
    prompt: str
    schema: object
    policy: object


def _protected_transport(
    system_prompt: str, prompt: str, schema: Mapping[str, object] | None
) -> _Transport | str:
    """Redacted inputs, or the failure class that blocks transport."""
    try:
        policy = load_policy()
        return _Transport(
            redact_for_transport(system_prompt, policy),
            redact_for_transport(prompt, policy),
            redact_transport_value(schema, policy),
            policy,
        )
    except DLPPolicyError:
        return "dlp_policy_error"
    except Exception:  # noqa: BLE001 - scanner failure must block transport
        return "dlp_scan_error"


def _counted_tokens(
    descriptor: ProviderDescriptor,
    transport: _Transport,
    native_schema_json: str | None,
    schema: Mapping[str, object] | None,
    mode: str,
    token_adapters: Mapping[str, TokenCounter] | None,
) -> TokenCount:
    """What we will send, counted — unless the native schema could not be shown."""
    if _schema_unshown(schema, mode, native_schema_json):
        return TokenCount()
    parts = [
        part
        for part in (transport.system_prompt, native_schema_json, transport.prompt)
        if part
    ]
    return count_tokens(
        "\n\n".join(parts), model=descriptor.model, adapters=token_adapters
    )


def _schema_unshown(
    schema: Mapping[str, object] | None, mode: str, native_schema_json: str | None
) -> bool:
    return schema is not None and mode == "native" and native_schema_json is None


def _invoked_backend(
    caller, descriptor: ProviderDescriptor, transport: _Transport, mode: str
):
    if mode == "native":
        return caller(
            descriptor, transport.prompt, transport.system_prompt, transport.schema
        )
    return caller(descriptor, transport.prompt, transport.system_prompt, None)


def _response_text_and_usage(response: object) -> tuple[object, TokenUsage]:
    if isinstance(response, BackendResponse):
        return response.text, response.usage
    return response, TokenUsage()


def _input_count(usage: TokenUsage, pre_call_count: TokenCount) -> TokenCount:
    if usage.input_tokens is not None:
        return TokenCount(usage.input_tokens, "reported")
    return pre_call_count


def _unsafe_output_failure(text: str, policy: object) -> str | None:
    try:
        require_safe_model_output(text, policy)
    except DLPContentBlocked:
        return "dlp_output_blocked"
    except Exception:  # noqa: BLE001 - scanner failure must block publication
        return "dlp_scan_error"
    return None


class ProviderTimeout(RuntimeError):
    """The provider was still working when its deadline passed.

    A deadline is not an answer. Collapsing it into the empty string made
    `_outcome_of` report `empty_response` — "the provider answered with
    nothing" — for a call that was never allowed to finish. That is the word
    the nightly pass of 2026-08-26 left behind (`draft:claude:<implicit>:
    empty_response` in `logs/nightly-2026-08-26.md`) for a daily log that
    compiled cleanly nine hours later, so the word did not describe what
    happened and nothing in the log could correct it.
    """


def _completed_call(
    descriptor: ProviderDescriptor,
    caller,
    transport: _Transport,
    mode: str,
    pre_call_count: TokenCount,
) -> LLMResult:
    try:
        response = _invoked_backend(caller, descriptor, transport, mode)
    except ProviderTimeout:
        print(
            f"llm_client: {descriptor.provider} backend exceeded "
            f"{_timeout_s()}s and was stopped",
            file=sys.stderr,
        )
        return LLMResult(
            descriptor,
            None,
            True,
            "provider_timeout",
            mode,
            TokenUsage(),
            pre_call_count,
        )
    except Exception as exc:  # noqa: BLE001 - providers must not crash callers
        print(
            f"llm_client: {descriptor.provider} backend failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return LLMResult(
            descriptor, None, True, "provider_error", mode, TokenUsage(), pre_call_count
        )
    return _outcome_of(descriptor, transport, mode, pre_call_count, response)


def _outcome_of(
    descriptor: ProviderDescriptor,
    transport: _Transport,
    mode: str,
    pre_call_count: TokenCount,
    response: object,
) -> LLMResult:
    text, usage = _response_text_and_usage(response)
    count = _input_count(usage, pre_call_count)
    if not isinstance(text, str) or not text.strip():
        return LLMResult(descriptor, None, True, "empty_response", mode, usage, count)
    failure = _unsafe_output_failure(text, transport.policy)
    if failure is not None:
        return LLMResult(descriptor, None, True, failure, mode, usage, count)
    return LLMResult(descriptor, text.strip(), True, None, mode, usage, count)


def _candidate_available(
    descriptor: ProviderDescriptor, available: bool | None
) -> bool:
    if available is None or descriptor.capabilities.get("local_only_enforced") is True:
        return probe_candidate(descriptor)
    return available


def _refused_candidate(
    descriptor: ProviderDescriptor,
    caller,
    mode: str,
    available: bool | None,
) -> LLMResult | None:
    """The two refusals that precede protecting the transport."""
    if caller is None:
        return LLMResult(descriptor, None, False, "unsupported", mode)
    if not _candidate_available(descriptor, available):
        return LLMResult(descriptor, None, False, "unavailable", mode)
    return None


def _dispatched_call(
    descriptor: ProviderDescriptor,
    prompt: str,
    system_prompt: str,
    schema: Mapping[str, object] | None,
    available: bool | None,
    token_adapters: Mapping[str, TokenCounter] | None,
) -> LLMResult:
    mode = _structured_mode(descriptor, schema)
    caller = _BACKENDS.get(descriptor.provider)
    refusal = _refused_candidate(descriptor, caller, mode, available)
    if refusal is not None:
        return refusal
    transport = _protected_transport(
        _prompted_system(system_prompt, schema, mode), prompt, schema
    )
    if isinstance(transport, str):
        return LLMResult(descriptor, None, False, transport, mode)
    native_schema_json = _native_schema_json(schema, mode)
    return _completed_call(
        descriptor,
        caller,
        transport,
        mode,
        _counted_tokens(
            descriptor, transport, native_schema_json, schema, mode, token_adapters
        ),
    )


def call_candidate(
    descriptor: ProviderDescriptor,
    prompt: str,
    system_prompt: str,
    *,
    max_tokens: int | None = None,
    schema: Mapping[str, object] | None = None,
    available: bool | None = None,
    token_adapters: Mapping[str, TokenCounter] | None = None,
) -> LLMResult:
    """Probe and call one resolved candidate, returning a stable outcome."""
    if descriptor.resolution_failure is not None:
        return LLMResult(
            descriptor, None, False, descriptor.resolution_failure, "prompt"
        )
    _require_token_contract(descriptor, max_tokens)
    return _dispatched_call(
        descriptor, prompt, system_prompt, schema, available, token_adapters
    )


def _llm_prompt_is_empty(prompt: str) -> bool:
    if not prompt:
        return True
    return not prompt.strip()


def _llm_result_is_terminal(result: LLMResult, forced: str) -> bool:
    if result.text is not None:
        return True
    return forced == "fake" and result.failure_class == "empty_response"


def _llm_fallback_item(result: LLMResult) -> str:
    failure = result.failure_class or "provider_error"
    return f"{result.descriptor.identity}:{failure}"


def call_llm_result(
    prompt: str, system_prompt: str = "", max_tokens: int = 2000
) -> LLMResult | None:
    """Return the successful provider outcome with its resolved identity."""
    if _llm_prompt_is_empty(prompt):
        return None

    forced = os.environ.get("MEMORY_LLM_PROVIDER", "").lower().strip()
    lineage: tuple[str, ...] = ()
    for candidate in provider_candidates(forced, max_tokens=max_tokens):
        descriptor = replace(candidate, fallback_from=lineage)
        result = call_candidate(
            descriptor,
            prompt,
            system_prompt,
            max_tokens=max_tokens,
        )
        if _llm_result_is_terminal(result, forced):
            return result
        lineage += (_llm_fallback_item(result),)

    return None


def call_llm(prompt: str, system_prompt: str = "", max_tokens: int = 2000) -> str | None:
    """Synchronous LLM call. Returns text, empty soft failure, or no backend."""
    if not prompt or not prompt.strip():
        return ""
    result = call_llm_result(prompt, system_prompt, max_tokens)
    if result is None:
        return None
    return result.text or ""



def call_llm_json(
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = 2000,
) -> str | None:
    """Call LLM with JSON-constraining instructions.

    Works with ALL existing providers (OpenAI, Claude, Codex, Ollama) by
    prepending a strict JSON-only instruction to the system prompt. No API
    parameter changes needed — the constraint is at the prompt level.

    Returns the LLM response (should be valid JSON). Callers should still
    parse defensively (json.loads + try/except) as LLMs occasionally
    add prose despite instructions.
    """
    json_instruction = (
        "CRITICAL: You MUST output ONLY valid JSON. No markdown, no prose, "
        "no code fences, no commentary. Start with { and end with }. "
        "If you cannot answer, output {\"error\": \"unable to respond\"}."
    )
    full_system = f"{system_prompt}\n\n{json_instruction}" if system_prompt else json_instruction
    return call_llm(prompt, full_system, max_tokens)


def _candidate_order(forced: str) -> list[str]:
    """Order in which to try backends.

    When ``forced`` is set to a known backend, ONLY that backend is tried —
    a strict override. If it fails, the call returns None rather than
    silently falling through to another provider. When ``forced`` is empty
    or unknown, the full default order is used (auto-detection).
    """
    defaults = ["opencode", "codex", "claude", "openai", "ollama"]
    if forced == "fake":
        return ["fake"]
    if forced and forced in defaults:
        return [forced]
    return defaults


ProviderConfiguration = tuple[
    "str | None", Mapping[str, object], Mapping[str, object], "str | None"
]


def _base_capabilities(provider: str) -> dict[str, object]:
    native = provider in {"openai", "ollama"}
    return {"structured_output": "native" if native else "prompt"}


def _cli_configuration(
    provider: str, model_variable: str, extra: Mapping[str, object] | None = None
) -> ProviderConfiguration:
    """A subscription CLI: the backend decides the token ceiling, not us."""
    capabilities = _base_capabilities(provider)
    capabilities["max_tokens_enforced"] = False
    settings: dict[str, object] = {"max_tokens": "backend_default"}
    settings.update(extra or {})
    return os.environ.get(model_variable) or None, capabilities, settings, None


def _http_configuration(
    provider: str, default_endpoint: str, default_model: str, max_tokens: int
) -> ProviderConfiguration:
    endpoint, endpoint_identity = _resolve_endpoint(
        os.environ.get("MEMORY_LLM_BASE_URL", default_endpoint)
    )
    capabilities = _base_capabilities(provider)
    capabilities["endpoint_sha256"] = endpoint_identity
    capabilities["max_tokens_enforced"] = True
    return (
        os.environ.get("MEMORY_LLM_MODEL", default_model),
        capabilities,
        {"max_tokens": max_tokens, "temperature_milli": 200},
        endpoint,
    )


def _fake_configuration(max_tokens: int) -> ProviderConfiguration:
    capabilities = _base_capabilities("fake")
    capabilities["max_tokens_enforced"] = True
    return "fake-v1", capabilities, {"max_tokens": max_tokens}, None


def _opencode_configuration() -> ProviderConfiguration:
    capabilities = _base_capabilities("opencode")
    capabilities["max_tokens_enforced"] = False
    return None, capabilities, {"max_tokens": "backend_default"}, None


_PROVIDER_CONFIGURATIONS = {
    "fake": lambda max_tokens: _fake_configuration(max_tokens),
    "opencode": lambda max_tokens: _opencode_configuration(),
    "codex": lambda max_tokens: _cli_configuration(
        "codex",
        "MEMORY_CODEX_MODEL",
        {"reasoning": os.environ.get("MEMORY_CODEX_REASONING", "low")},
    ),
    "claude": lambda max_tokens: _cli_configuration("claude", "MEMORY_CLAUDE_MODEL"),
    "openai": lambda max_tokens: _http_configuration(
        "openai", "https://api.openai.com/v1", "gpt-4o-mini", max_tokens
    ),
    "ollama": lambda max_tokens: _http_configuration(
        "ollama", "http://localhost:11434/v1", "qwen3:0.6b", max_tokens
    ),
}


def _provider_configuration(provider: str, max_tokens: int) -> ProviderConfiguration:
    build = _PROVIDER_CONFIGURATIONS.get(provider)
    if build is None:
        return None, _base_capabilities(provider), {"max_tokens": max_tokens}, None
    return build(max_tokens)


def _split_endpoint(endpoint: str):
    try:
        return urllib.parse.urlsplit(endpoint)
    except ValueError as exc:
        raise ValueError("MEMORY_LLM_BASE_URL is not a valid HTTP endpoint") from exc


def _require_plain_endpoint(parsed, endpoint: str) -> None:
    """No credentials, no query, no fragment: an endpoint, not a request."""
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "MEMORY_LLM_BASE_URL must not contain userinfo, query, or fragment"
        )
    if "?" in endpoint or "#" in endpoint:
        raise ValueError(
            "MEMORY_LLM_BASE_URL must not contain userinfo, query, or fragment"
        )


def _bracketed_host(hostname: str) -> str:
    lowered = hostname.casefold()
    if ":" in lowered:
        return f"[{lowered}]"
    return lowered


def _resolve_endpoint(endpoint: str) -> tuple[str, str]:
    parsed = _split_endpoint(endpoint)
    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname
    if scheme not in {"http", "https"} or not hostname:
        raise ValueError("MEMORY_LLM_BASE_URL must use HTTP(S) with a hostname")
    _require_plain_endpoint(parsed, endpoint)
    effective_port = _endpoint_port(parsed, scheme)
    normalized = (
        f"{scheme}://{_bracketed_host(hostname)}:{effective_port}"
        f"{parsed.path.rstrip('/')}"
    )
    return normalized, hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _endpoint_port(parsed, scheme: str) -> int:
    if parsed.port:
        return parsed.port
    if scheme == "https":
        return 443
    return 80


def _is_literal_loopback_endpoint(endpoint: str) -> bool:
    try:
        hostname = urllib.parse.urlsplit(endpoint).hostname
    except ValueError:
        return False
    return hostname in {"127.0.0.1", "::1"}


# ---------------------------------------------------------------------------
# Liveness probes (cheap, before attempting real call)
# ---------------------------------------------------------------------------


def _probe_opencode(descriptor: ProviderDescriptor) -> bool:
    """Is OpenCode server alive on localhost:4096 (or OPENCODE_PORT)?"""
    port = int(os.environ.get("OPENCODE_PORT", "4096"))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            # Socket open — confirm it's actually OpenCode via /health.
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                return resp.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _probe_codex(descriptor: ProviderDescriptor) -> bool:
    return _find_codex_binary() is not None


def _probe_claude(descriptor: ProviderDescriptor) -> bool:
    return shutil.which("claude") is not None


def _probe_openai(descriptor: ProviderDescriptor) -> bool:
    return bool(
        os.environ.get("MEMORY_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    )


def _ollama_tags_url(endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    tags_path = f"{path}/api/tags" if path else "/api/tags"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, tags_path, "", ""))


def _ollama_lists_the_local_model(descriptor: ProviderDescriptor, response) -> bool:
    raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        return False
    return _ollama_has_local_model(json.loads(raw.decode("utf-8")), descriptor.model)


def _ollama_tags_answer(descriptor: ProviderDescriptor, response) -> bool:
    if response.status != 200:
        return False
    if descriptor.capabilities.get("local_only_enforced") is not True:
        return True
    return _ollama_lists_the_local_model(descriptor, response)


def _probe_ollama(descriptor: ProviderDescriptor) -> bool:
    if descriptor._endpoint is None:
        return False
    try:
        request = urllib.request.Request(_ollama_tags_url(descriptor._endpoint))
        with urllib.request.urlopen(request, timeout=1.0) as response:
            return _ollama_tags_answer(descriptor, response)
    except (
        json.JSONDecodeError,
        OSError,
        UnicodeDecodeError,
        ValueError,
        urllib.error.URLError,
    ):
        return False


def _is_local_ollama_entry(item: Mapping) -> bool:
    """A model that lives on this machine: real bytes, a real digest, no remote."""
    if item.get("remote_model") or item.get("remote_host"):
        return False
    return _has_local_size(item.get("size")) and _has_digest(item.get("digest"))


def _has_local_size(size: object) -> bool:
    return isinstance(size, int) and not isinstance(size, bool) and size > 0


def _has_digest(digest: object) -> bool:
    return isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None


def _named_ollama_entry(models: object, model: str) -> Mapping | None:
    for item in models if isinstance(models, list) else []:
        if isinstance(item, Mapping) and model in {item.get("name"), item.get("model")}:
            return item
    return None


def _ollama_has_local_model(payload: object, model: str | None) -> bool:
    if not isinstance(payload, Mapping) or not isinstance(model, str) or not model:
        return False
    item = _named_ollama_entry(payload.get("models"), model)
    if item is None:
        return False
    return _is_local_ollama_entry(item)


def _probe_fake(descriptor: ProviderDescriptor) -> bool:
    return True


_PROBES = {
    "fake": _probe_fake,
    "opencode": _probe_opencode,
    "codex": _probe_codex,
    "claude": _probe_claude,
    "openai": _probe_openai,
    "ollama": _probe_ollama,
}


def _timeout_s() -> int:
    return int(os.environ.get("MEMORY_LLM_TIMEOUT_S", "90"))


def _reported_count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _finite_number(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _usage_from_counts(
    *,
    input_tokens: object = None,
    output_tokens: object = None,
    cache_read_tokens: object = None,
    cache_write_tokens: object = None,
    duration_ms: object = None,
) -> TokenUsage:
    return TokenUsage(
        input_tokens=_reported_count(input_tokens),
        output_tokens=_reported_count(output_tokens),
        cache_read_tokens=_reported_count(cache_read_tokens),
        cache_write_tokens=_reported_count(cache_write_tokens),
        duration_ms=_reported_count(duration_ms),
    )


def _parse_http_usage(data: object) -> TokenUsage:
    if not isinstance(data, Mapping):
        return TokenUsage()
    usage = data.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, Mapping) else {}
    return _usage_from_counts(
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        cache_read_tokens=details.get("cached_tokens"),
    )


def _usage_from_opencode_tokens(tokens: object) -> TokenUsage:
    if not isinstance(tokens, Mapping):
        return TokenUsage()
    cache = tokens.get("cache")
    cache = cache if isinstance(cache, Mapping) else {}
    return _usage_from_counts(
        input_tokens=tokens.get("input"),
        output_tokens=tokens.get("output"),
        cache_read_tokens=cache.get("read"),
        cache_write_tokens=cache.get("write"),
    )


def _mapping_or_empty(value: object) -> Mapping:
    if isinstance(value, Mapping):
        return value
    return {}


def _step_finish_tokens(part: object) -> Mapping | None:
    if not isinstance(part, Mapping) or part.get("type") != "step-finish":
        return None
    tokens = part.get("tokens")
    if not isinstance(tokens, Mapping):
        return None
    return tokens


def _summed_part_usage(parts: object) -> TokenUsage:
    """Usage added up over the finished steps, when no total is reported."""
    totals: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
    }
    for part in parts if isinstance(parts, list) else []:
        tokens = _step_finish_tokens(part)
        if tokens is None:
            continue
        _add_usage(totals, _usage_from_opencode_tokens(tokens))
    return TokenUsage(**totals)


def _add_usage(totals: dict[str, int | None], usage: TokenUsage) -> None:
    for name in totals:
        value = getattr(usage, name)
        if value is None:
            continue
        totals[name] = (totals[name] or 0) + value


def _reported_usage(info: Mapping, root: Mapping) -> TokenUsage:
    if isinstance(info.get("tokens"), Mapping):
        return _usage_from_opencode_tokens(info["tokens"])
    return _summed_part_usage(root.get("parts"))


def _reported_cost(info: Mapping) -> float | None:
    cost = _finite_number(info.get("cost"))
    if cost is None or cost < 0:
        return None
    return cost


def _ordered_instants(info: Mapping) -> tuple[float, float] | None:
    time = _mapping_or_empty(info.get("time"))
    created = _finite_number(time.get("created"))
    completed = _finite_number(time.get("completed"))
    if created is None or completed is None or created < 0 or completed < created:
        return None
    return created, completed


def _elapsed_ms(info: Mapping) -> int | None:
    """Milliseconds between the reported creation and completion instants."""
    instants = _ordered_instants(info)
    if instants is None:
        return None
    delta = _finite_number(instants[1] - instants[0])
    if delta is None or delta < 0:
        return None
    return math.floor(delta)


def _parse_opencode_usage(data: object) -> TokenUsage:
    if not isinstance(data, Mapping):
        return TokenUsage()
    root = data.get("data") if isinstance(data.get("data"), Mapping) else data
    info = _mapping_or_empty(root.get("info"))
    token_usage = _reported_usage(info, root)
    cost = _reported_cost(info)
    return TokenUsage(
        input_tokens=token_usage.input_tokens,
        output_tokens=token_usage.output_tokens,
        cache_read_tokens=token_usage.cache_read_tokens,
        cache_write_tokens=token_usage.cache_write_tokens,
        duration_ms=_elapsed_ms(info),
        estimated_cost=cost,
        cost_kind="reported" if cost is not None else "unknown",
    )


# ---------------------------------------------------------------------------
# Backend 1: OpenCode server (HTTP API) — uses your OpenCode subscription
# ---------------------------------------------------------------------------


def _opencode_post(url: str, payload: Mapping[str, object]) -> object:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=_timeout_s()) as response:
        raw = response.read().decode("utf-8")
    if not raw:
        return None
    return json.loads(raw)


def _opencode_nested_id(data: Mapping) -> str:
    nested = data.get("data")
    if isinstance(nested, Mapping) and nested.get("id"):
        return str(nested["id"])
    return ""


def _opencode_session_id(data: object) -> str:
    """Servers answer either {id} or {data: {id}}."""
    if not isinstance(data, dict):
        return ""
    direct = data.get("id")
    if direct:
        return str(direct)
    return _opencode_nested_id(data)


def _opencode_root(data: object) -> object:
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data


def _opencode_parts(data: object) -> list:
    root = _opencode_root(data)
    if not isinstance(root, Mapping):
        return []
    parts = root.get("parts", [])
    return parts if isinstance(parts, list) else []


def _opencode_text(data: object) -> str:
    return "\n".join(
        str(part["text"]) for part in _opencode_parts(data) if _is_text_part(part)
    )


def _is_text_part(part: object) -> bool:
    if not isinstance(part, Mapping) or part.get("type") != "text":
        return False
    return isinstance(part.get("text"), str)


def _opencode_delete(base: str, session_id: str) -> None:
    try:
        request = urllib.request.Request(
            f"{base}/session/{session_id}", method="DELETE"
        )
        urllib.request.urlopen(request, timeout=5.0)
    except (urllib.error.URLError, OSError):
        pass


def _opencode_answer(base: str, session_id: str, prompt: str, system_prompt: str):
    if system_prompt:
        _opencode_post(
            f"{base}/session/{session_id}/prompt",
            {"noReply": True, "parts": [{"type": "text", "text": system_prompt}]},
        )
    data = _opencode_post(
        f"{base}/session/{session_id}/prompt",
        {"parts": [{"type": "text", "text": prompt}]},
    )
    return BackendResponse(_opencode_text(data), _parse_opencode_usage(data))


def _call_opencode(
    descriptor: ProviderDescriptor,
    prompt: str,
    system_prompt: str,
    schema: Mapping[str, object] | None = None,
) -> str | BackendResponse:
    """Call OpenCode's HTTP API: create session → prompt → read → delete."""
    port = int(os.environ.get("OPENCODE_PORT", "4096"))
    base = f"http://127.0.0.1:{port}"
    session_id = _opencode_session_id(
        _opencode_post(f"{base}/session", {"title": "memory-pipeline-ephemeral"})
    )
    if not session_id:
        return ""
    try:
        return _opencode_answer(base, session_id, prompt, system_prompt)
    finally:
        _opencode_delete(base, session_id)


# ---------------------------------------------------------------------------
# Backend 2: Codex CLI — uses your Codex subscription
# ---------------------------------------------------------------------------


def _windows_codex_candidate() -> str | None:
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    for ext in (".cmd", ".ps1", ".exe"):
        candidate = Path(appdata) / "npm" / f"codex{ext}"
        if candidate.exists():
            return str(candidate)
    return None


def _find_codex_binary() -> str | None:
    """Locate the codex executable. Returns path or None."""
    found = shutil.which("codex")
    if found:
        return found
    if sys.platform != "win32":
        return None
    return _windows_codex_candidate()


def _codex_command(codex_bin: str, model: str | None, reasoning: str, out_path: str) -> list[str]:
    command = [
        codex_bin,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-c",
        f"model_reasoning_effort={reasoning}",
        "--output-last-message",
        out_path,
    ]
    if model:
        command.extend(["-m", model])
    return command


def _temp_text_file(content: str = "") -> str:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(content)
        return handle.name


def _remove_quietly(paths: tuple[str, ...]) -> None:
    for path in paths:
        try:
            Path(path).unlink()
        except OSError:
            pass


def _codex_last_message(command: list[str], prompt_path: str, out_path: str) -> str:
    with open(prompt_path, "rb") as stdin_handle:
        subprocess.run(
            command,
            stdin=stdin_handle,
            capture_output=True,
            timeout=_timeout_s(),
            check=False,
        )
    try:
        return Path(out_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _call_codex(
    descriptor: ProviderDescriptor,
    prompt: str,
    system_prompt: str,
    schema: Mapping[str, object] | None = None,
) -> str:
    """Call `codex exec` and return the model's final message."""
    codex_bin = _find_codex_binary()
    if not codex_bin:
        return ""
    combined = prompt
    if system_prompt:
        combined = f"SYSTEM: {system_prompt}\n\n---\n\nUSER: {prompt}"
    prompt_path = _temp_text_file(combined)
    out_path = _temp_text_file()
    command = _codex_command(
        codex_bin,
        descriptor.model,
        str(descriptor.inference_settings.get("reasoning", "low")),
        out_path,
    )
    try:
        return _codex_last_message(command, prompt_path, out_path)
    finally:
        _remove_quietly((prompt_path, out_path))


# ---------------------------------------------------------------------------
# Backend 3: Claude CLI — uses your Claude subscription
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _claude_cli_flags() -> frozenset[str]:
    """Which flags this Claude CLI understands, asked once per process."""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return frozenset()
    try:
        result = subprocess.run(
            [claude_bin, "--help"],
            capture_output=True,
            timeout=30,
            check=False,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
    except (subprocess.TimeoutExpired, OSError):
        return frozenset()
    return frozenset(re.findall(r"--[a-z][a-z-]+", result.stdout or ""))


def _claude_command(claude_bin: str, model: str | None, system_prompt: str) -> list[str]:
    """The call, isolated from the operator's interactive persona.

    `claude -p` loads the user's settings, including the output style, and
    treats a `<system>` block in the prompt as ordinary text. Measured on this
    machine: the compile's draft came back as a chat reply in the operator's
    configured voice — "От вас ничего не нужно" — instead of the JSON plan the
    schema asked for, three times in a row. `--system-prompt` replaces the
    assistant persona with ours; `--setting-sources` with nothing after it loads
    no settings files at all. Both are used only when this CLI has them.
    """
    flags = _claude_cli_flags()
    optional = (
        (
            bool(system_prompt) and "--system-prompt" in flags,
            ["--system-prompt", system_prompt],
        ),
        ("--setting-sources" in flags, ["--setting-sources", ""]),
        (bool(model), ["--model", str(model)]),
    )
    command = [claude_bin, "-p", "--output-format", "text"]
    for applies, argument in optional:
        if applies:
            command.extend(argument)
    return command


def _claude_stdin(system_prompt: str, prompt: str) -> str:
    """The prompt, carrying the system text only when the flag cannot."""
    if not system_prompt or "--system-prompt" in _claude_cli_flags():
        return prompt
    return f"<system>{system_prompt}</system>\n\n{prompt}"


def _call_claude(
    descriptor: ProviderDescriptor,
    prompt: str,
    system_prompt: str,
    schema: Mapping[str, object] | None = None,
) -> str:
    """Call `claude -p` (print mode, non-interactive) and return the response.

    Claude Code's `-p` flag runs a one-shot prompt and exits. Pair with
    `--output-format text` for clean text output. Uses your Claude
    subscription auth (same login as `claude` interactive TUI). The prompt goes
    through stdin to avoid the Windows CreateProcess ~32K command-line ceiling.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return ""
    try:
        result = subprocess.run(
            _claude_command(claude_bin, descriptor.model, system_prompt),
            input=_claude_stdin(system_prompt, prompt),
            capture_output=True,
            timeout=_timeout_s(),
            check=False,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        return result.stdout or ""
    except subprocess.TimeoutExpired as exc:
        raise ProviderTimeout(
            f"claude did not answer within {_timeout_s()}s"
        ) from exc


# ---------------------------------------------------------------------------
# Backend 4: OpenAI-compatible HTTP API (optional, paid)
# ---------------------------------------------------------------------------


def _openai_messages(system_prompt: str, prompt: str) -> list[dict[str, str]]:
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages


def _openai_payload(
    descriptor: ProviderDescriptor,
    prompt: str,
    system_prompt: str,
    schema: Mapping[str, object] | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": descriptor.model,
        "messages": _openai_messages(system_prompt, prompt),
        "max_tokens": int(descriptor.inference_settings["max_tokens"]),
        "temperature": int(descriptor.inference_settings["temperature_milli"]) / 1000,
    }
    if schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "memory_response",
                "strict": True,
                "schema": schema,
            },
        }
    return payload


def _call_openai(
    descriptor: ProviderDescriptor,
    prompt: str,
    system_prompt: str,
    schema: Mapping[str, object] | None = None,
) -> str | BackendResponse:
    api_key = os.environ.get("MEMORY_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return ""
    if descriptor._endpoint is None:
        raise ValueError("OpenAI endpoint was not resolved in the provider descriptor")
    body = json.dumps(
        _openai_payload(descriptor, prompt, system_prompt, schema)
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{descriptor._endpoint.rstrip('/')}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_timeout_s()) as response:
        data = json.loads(response.read().decode("utf-8"))
    return BackendResponse(
        data["choices"][0]["message"]["content"], _parse_http_usage(data)
    )


# ---------------------------------------------------------------------------
# Backend 5: Ollama HTTP API (optional, local, free, offline)
# ---------------------------------------------------------------------------


def _ollama_messages(system_prompt: str, prompt: str) -> list[dict[str, str]]:
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages


def _ollama_payload(
    descriptor: ProviderDescriptor,
    prompt: str,
    system_prompt: str,
    schema: Mapping[str, object] | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": descriptor.model,
        "messages": _ollama_messages(system_prompt, prompt),
        "max_tokens": int(descriptor.inference_settings["max_tokens"]),
        "temperature": int(descriptor.inference_settings["temperature_milli"]) / 1000,
        "stream": False,
    }
    if schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "memory_response",
                "strict": True,
                "schema": schema,
            },
        }
    return payload


def _call_ollama(
    descriptor: ProviderDescriptor,
    prompt: str,
    system_prompt: str,
    schema: Mapping[str, object] | None = None,
) -> str | BackendResponse:
    if descriptor._endpoint is None:
        raise ValueError("Ollama endpoint was not resolved in the provider descriptor")
    base_url = descriptor._endpoint
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = json.dumps(
        _ollama_payload(descriptor, prompt, system_prompt, schema)
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_timeout_s()) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return BackendResponse(
        data["choices"][0]["message"]["content"],
        _parse_http_usage(data),
    )


# Backend registry.
def _call_fake(
    descriptor: ProviderDescriptor,
    prompt: str,
    system_prompt: str,
    schema: Mapping[str, object] | None = None,
) -> str:
    return os.environ.get(
        "MEMORY_LLM_FAKE_RESPONSE",
        '{"operations": [], "audit": {"verified": 0, "dedup": 0, "stubs": 0, '
        '"contradictions": 0, "rejected": 0}}\nCOMPILE_AUDIT: verified 0 evidence '
        "citations; 0 dedup checks performed; 0 stubs skipped; 0 contradictions handled; "
        "0 pages rejected as below-threshold",
    ).strip()


_BACKENDS = {
    "fake": _call_fake,
    "opencode": _call_opencode,
    "codex": _call_codex,
    "claude": _call_claude,
    "openai": _call_openai,
    "ollama": _call_ollama,
}


# ---------------------------------------------------------------------------
# CLI for testing / debugging
# ---------------------------------------------------------------------------


def _backend_alive(name: str) -> bool:
    try:
        return probe_candidate(provider_candidates(name)[0])
    except Exception:  # noqa: BLE001 - a broken probe is "not available"
        return False


def _print_availability() -> None:
    for name in _PROBES:
        alive = "ALIVE" if _backend_alive(name) else "not available"
        print(f"  {name}: {alive}", file=sys.stderr)


def _cli() -> int:
    """Quick CLI: `python llm_client.py "your prompt"` to test backends."""
    if len(sys.argv) < 2:
        print('Usage: python llm_client.py "<prompt>"', file=sys.stderr)
        print("\nBackend availability:", file=sys.stderr)
        _print_availability()
        return 1
    prompt = sys.argv[1]
    system = sys.argv[2] if len(sys.argv) > 2 else ""
    print("--- backend availability ---", file=sys.stderr)
    _print_availability()
    print("--- calling first alive backend ---", file=sys.stderr)
    response = call_llm(prompt, system)
    print(response)
    return 0 if response else 2


if __name__ == "__main__":
    raise SystemExit(_cli())
