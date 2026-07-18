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

import hashlib
import json
import math
import os
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

from context_budget import TokenCount, TokenCounter, TokenUsage, count_tokens
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


def provider_candidates(
    forced: str = "",
    *,
    max_tokens: int = 2000,
) -> list[ProviderDescriptor]:
    """Resolve ordered provider identities without probing or calling them."""
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    candidates = _candidate_order(forced.lower().strip())
    descriptors = []
    for index, provider in enumerate(candidates):
        try:
            model, capabilities, settings, endpoint = _provider_configuration(
                provider, max_tokens
            )
        except ValueError:
            descriptors.append(
                ProviderDescriptor(
                    provider=provider,
                    model=None,
                    capabilities=MappingProxyType({}),
                    inference_settings=MappingProxyType({}),
                    candidate_index=index,
                    fallback_from=(),
                    _resolution_failure="invalid_configuration",
                )
            )
            continue
        descriptors.append(
            ProviderDescriptor(
                provider=provider,
                model=model,
                capabilities=MappingProxyType(dict(capabilities)),
                inference_settings=MappingProxyType(dict(settings)),
                candidate_index=index,
                fallback_from=(),
                _endpoint=endpoint,
            )
        )
    return descriptors


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
            descriptor,
            None,
            False,
            descriptor.resolution_failure,
            "prompt",
        )
    effective_max_tokens = descriptor.inference_settings.get("max_tokens")
    max_tokens_enforced = descriptor.capabilities.get("max_tokens_enforced")
    if max_tokens_enforced is True:
        if (
            not isinstance(effective_max_tokens, int)
            or isinstance(effective_max_tokens, bool)
            or effective_max_tokens <= 0
        ):
            raise ValueError("descriptor max_tokens must be a positive integer")
        if max_tokens is not None and max_tokens != effective_max_tokens:
            raise ValueError("max_tokens does not match the resolved provider descriptor")
    elif max_tokens_enforced is False:
        if effective_max_tokens != "backend_default":
            raise ValueError("descriptor must record the backend token default")
        if max_tokens is not None and (
            not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0
        ):
            raise ValueError("max_tokens request must be a positive integer")
    else:
        raise ValueError("descriptor must declare whether max_tokens is enforced")
    structured_output = (
        "native"
        if schema is not None
        and descriptor.capabilities.get("structured_output") == "native"
        else "prompt"
    )
    caller = _BACKENDS.get(descriptor.provider)
    if caller is None:
        return LLMResult(descriptor, None, False, "unsupported", structured_output)
    if available is None:
        available = probe_candidate(descriptor)
    if not available:
        return LLMResult(descriptor, None, False, "unavailable", structured_output)

    call_system_prompt = system_prompt
    if schema is not None and structured_output == "prompt":
        schema_json = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        instruction = f"Output only JSON matching this schema: {schema_json}"
        call_system_prompt = (
            f"{system_prompt}\n\n{instruction}" if system_prompt else instruction
        )
    native_schema_json = None
    if schema is not None and structured_output == "native":
        try:
            native_schema_json = json.dumps(
                schema, sort_keys=True, separators=(",", ":")
            )
        except Exception:  # noqa: BLE001 - counting must not replace provider validation
            pass
    counted_parts = [part for part in (call_system_prompt, native_schema_json, prompt) if part]
    pre_call_count = (
        count_tokens(
            "\n\n".join(counted_parts),
            model=descriptor.model,
            adapters=token_adapters,
        )
        if schema is None or structured_output != "native" or native_schema_json is not None
        else TokenCount()
    )
    try:
        if structured_output == "native":
            response = caller(descriptor, prompt, call_system_prompt, schema)
        else:
            response = caller(descriptor, prompt, call_system_prompt, None)
    except Exception as exc:  # noqa: BLE001 - providers must not crash callers
        print(
            f"llm_client: {descriptor.provider} backend failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return LLMResult(
            descriptor,
            None,
            True,
            "provider_error",
            structured_output,
            TokenUsage(),
            pre_call_count,
        )
    if isinstance(response, BackendResponse):
        text = response.text
        usage = response.usage
    else:
        text = response
        usage = TokenUsage()
    input_token_count = (
        TokenCount(usage.input_tokens, "reported")
        if usage.input_tokens is not None
        else pre_call_count
    )
    if not isinstance(text, str) or not text.strip():
        return LLMResult(
            descriptor,
            None,
            True,
            "empty_response",
            structured_output,
            usage,
            input_token_count,
        )
    return LLMResult(
        descriptor,
        text.strip(),
        True,
        None,
        structured_output,
        usage,
        input_token_count,
    )

def call_llm(prompt: str, system_prompt: str = "", max_tokens: int = 2000) -> str | None:
    """Synchronous LLM call. Returns response text, "" on soft failure,
    or None when no backend is available.

    When no backend is available, None is returned. Callers treat this
    gracefully (compile skips, flush treats as FLUSH_OK, query returns
    error string). The queue (``scripts/memory_queue.py``) is available
    as an explicit API for callers that want deferred execution.
    """
    if not prompt or not prompt.strip():
        return ""

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
        if result.text is not None:
            return result.text
        if forced == "fake" and result.failure_class == "empty_response":
            return ""
        failure = result.failure_class or "provider_error"
        lineage += (f"{descriptor.identity}:{failure}",)

    # No backend available — return None. Callers handle this gracefully
    # (compile skips, flush treats as FLUSH_OK, query returns error string).
    # The queue is available as an explicit API for deferred execution.
    return None


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


def _provider_configuration(
    provider: str,
    max_tokens: int,
) -> tuple[str | None, Mapping[str, object], Mapping[str, object], str | None]:
    native = provider in {"openai", "ollama"}
    capabilities: Mapping[str, object] = {
        "structured_output": "native" if native else "prompt",
    }
    if provider == "fake":
        capabilities = dict(capabilities)
        capabilities["max_tokens_enforced"] = True
        return "fake-v1", capabilities, {"max_tokens": max_tokens}, None
    if provider == "opencode":
        capabilities = dict(capabilities)
        capabilities["max_tokens_enforced"] = False
        return None, capabilities, {"max_tokens": "backend_default"}, None
    if provider == "codex":
        capabilities = dict(capabilities)
        capabilities["max_tokens_enforced"] = False
        return (
            os.environ.get("MEMORY_CODEX_MODEL") or None,
            capabilities,
            {
                "max_tokens": "backend_default",
                "reasoning": os.environ.get("MEMORY_CODEX_REASONING", "low"),
            },
            None,
        )
    if provider == "claude":
        capabilities = dict(capabilities)
        capabilities["max_tokens_enforced"] = False
        return (
            os.environ.get("MEMORY_CLAUDE_MODEL") or None,
            capabilities,
            {"max_tokens": "backend_default"},
            None,
        )
    if provider == "openai":
        endpoint, endpoint_identity = _resolve_endpoint(
            os.environ.get("MEMORY_LLM_BASE_URL", "https://api.openai.com/v1")
        )
        capabilities = dict(capabilities)
        capabilities["endpoint_sha256"] = endpoint_identity
        capabilities["max_tokens_enforced"] = True
        return (
            os.environ.get("MEMORY_LLM_MODEL", "gpt-4o-mini"),
            capabilities,
            {"max_tokens": max_tokens, "temperature_milli": 200},
            endpoint,
        )
    if provider == "ollama":
        endpoint, endpoint_identity = _resolve_endpoint(
            os.environ.get("MEMORY_LLM_BASE_URL", "http://localhost:11434/v1")
        )
        capabilities = dict(capabilities)
        capabilities["endpoint_sha256"] = endpoint_identity
        capabilities["max_tokens_enforced"] = True
        return (
            os.environ.get("MEMORY_LLM_MODEL", "qwen3:0.6b"),
            capabilities,
            {"max_tokens": max_tokens, "temperature_milli": 200},
            endpoint,
        )
    return None, capabilities, {"max_tokens": max_tokens}, None


def _resolve_endpoint(endpoint: str) -> tuple[str, str]:
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        scheme = parsed.scheme.casefold()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("MEMORY_LLM_BASE_URL is not a valid HTTP endpoint") from exc
    if scheme not in {"http", "https"} or not hostname:
        raise ValueError("MEMORY_LLM_BASE_URL must use HTTP(S) with a hostname")
    if (
        parsed.username is not None
        or parsed.password is not None
        or "?" in endpoint
        or "#" in endpoint
    ):
        raise ValueError("MEMORY_LLM_BASE_URL must not contain userinfo, query, or fragment")
    hostname = hostname.casefold()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    effective_port = port or (443 if scheme == "https" else 80)
    path = parsed.path.rstrip("/")
    normalized = f"{scheme}://{hostname}:{effective_port}{path}"
    return normalized, hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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


def _probe_ollama(descriptor: ProviderDescriptor) -> bool:
    if descriptor._endpoint is None:
        return False
    try:
        parsed = urllib.parse.urlsplit(descriptor._endpoint)
        path = parsed.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        tags_path = f"{path}/api/tags" if path else "/api/tags"
        tags_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, tags_path, "", "")
        )
        request = urllib.request.Request(tags_url)
        with urllib.request.urlopen(request, timeout=1.0) as response:
            return response.status == 200
    except (OSError, ValueError, urllib.error.URLError):
        return False


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


def _parse_opencode_usage(data: object) -> TokenUsage:
    if not isinstance(data, Mapping):
        return TokenUsage()
    root = data.get("data") if isinstance(data.get("data"), Mapping) else data
    info = root.get("info") if isinstance(root.get("info"), Mapping) else {}
    if isinstance(info.get("tokens"), Mapping):
        token_usage = _usage_from_opencode_tokens(info["tokens"])
    else:
        totals: dict[str, int | None] = {
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
        }
        parts = root.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if (
                    not isinstance(part, Mapping)
                    or part.get("type") != "step-finish"
                    or not isinstance(part.get("tokens"), Mapping)
                ):
                    continue
                part_usage = _usage_from_opencode_tokens(part["tokens"])
                for name in totals:
                    value = getattr(part_usage, name)
                    if value is not None:
                        totals[name] = (totals[name] or 0) + value
        token_usage = TokenUsage(**totals)

    cost = _finite_number(info.get("cost"))
    if cost is not None and cost < 0:
        cost = None
    time = info.get("time") if isinstance(info.get("time"), Mapping) else {}
    created = _finite_number(time.get("created"))
    completed = _finite_number(time.get("completed"))
    delta = (
        _finite_number(completed - created)
        if created is not None
        and completed is not None
        and created >= 0
        and completed >= 0
        and completed >= created
        else None
    )
    duration_ms = math.floor(delta) if delta is not None and delta >= 0 else None
    return TokenUsage(
        input_tokens=token_usage.input_tokens,
        output_tokens=token_usage.output_tokens,
        cache_read_tokens=token_usage.cache_read_tokens,
        cache_write_tokens=token_usage.cache_write_tokens,
        duration_ms=duration_ms,
        estimated_cost=cost,
        cost_kind="reported" if cost is not None else "unknown",
    )


# ---------------------------------------------------------------------------
# Backend 1: OpenCode server (HTTP API) — uses your OpenCode subscription
# ---------------------------------------------------------------------------


def _call_opencode(
    descriptor: ProviderDescriptor,
    prompt: str,
    system_prompt: str,
    schema: Mapping[str, object] | None = None,
) -> str | BackendResponse:
    """Call OpenCode's HTTP API: create session → prompt → read → delete."""
    port = int(os.environ.get("OPENCODE_PORT", "4096"))
    base = f"http://127.0.0.1:{port}"

    # 1. Create an ephemeral session.
    body = json.dumps({"title": "memory-pipeline-ephemeral"}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/session",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_timeout_s()) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    session_id = data.get("id") if isinstance(data, dict) else None
    if not session_id:
        # Some servers return {data: {id: ...}}.
        session_id = (data.get("data") or {}).get("id")
    if not session_id:
        return ""

    try:
        # 2. Inject system prompt as no-reply context.
        if system_prompt:
            body = json.dumps(
                {"noReply": True, "parts": [{"type": "text", "text": system_prompt}]}
            ).encode("utf-8")
            req = urllib.request.Request(
                f"{base}/session/{session_id}/prompt",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_timeout_s()):
                pass

        # 3. Real prompt.
        body = json.dumps(
            {"parts": [{"type": "text", "text": prompt}]}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/session/{session_id}/prompt",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_timeout_s()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # Extract text from response. Shape varies: {data: {parts: [...]}}
        # or {parts: [...]} or {data: {info: ..., parts: [...]}}.
        parts_root = data
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            parts_root = data["data"]
        parts = parts_root.get("parts", []) if isinstance(parts_root, Mapping) else []
        text = "\n".join(
            part["text"]
            for part in parts
            if isinstance(part, Mapping)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
        return BackendResponse(text, _parse_opencode_usage(data))
    finally:
        # 4. Delete session — best-effort cleanup.
        try:
            req = urllib.request.Request(
                f"{base}/session/{session_id}", method="DELETE"
            )
            urllib.request.urlopen(req, timeout=5.0)
        except (urllib.error.URLError, OSError):
            pass


# ---------------------------------------------------------------------------
# Backend 2: Codex CLI — uses your Codex subscription
# ---------------------------------------------------------------------------


def _find_codex_binary() -> str | None:
    """Locate the codex executable. Returns path or None."""
    found = shutil.which("codex")
    if found:
        return found
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            for ext in (".cmd", ".ps1", ".exe"):
                candidate = Path(appdata) / "npm" / f"codex{ext}"
                if candidate.exists():
                    return str(candidate)
    return None


def _call_codex(
    descriptor: ProviderDescriptor,
    prompt: str,
    system_prompt: str,
    schema: Mapping[str, object] | None = None,
) -> str:
    """Call `codex exec` and return the model's final message."""
    reasoning = str(descriptor.inference_settings.get("reasoning", "low"))
    model = descriptor.model
    codex_bin = _find_codex_binary()
    if not codex_bin:
        return ""

    combined = prompt
    if system_prompt:
        combined = f"SYSTEM: {system_prompt}\n\n---\n\nUSER: {prompt}"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as prompt_file:
        prompt_file.write(combined)
        prompt_path = prompt_file.name

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as out_file:
        out_path = out_file.name

    try:
        cmd = [
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
            cmd.extend(["-m", model])

        with open(prompt_path, "rb") as stdin_handle:
            subprocess.run(
                cmd,
                stdin=stdin_handle,
                capture_output=True,
                timeout=_timeout_s(),
                check=False,
            )
        try:
            return Path(out_path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""
    finally:
        for p in (prompt_path, out_path):
            try:
                Path(p).unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Backend 3: Claude CLI — uses your Claude subscription
# ---------------------------------------------------------------------------


def _call_claude(
    descriptor: ProviderDescriptor,
    prompt: str,
    system_prompt: str,
    schema: Mapping[str, object] | None = None,
) -> str:
    """Call `claude -p` (print mode, non-interactive) and return the response.

    Claude Code's `-p` flag runs a one-shot prompt and exits. Pair with
    `--output-format text` for clean text output. Uses your Claude
    subscription auth (same login as `claude` interactive TUI).
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return ""

    # Claude CLI accepts the prompt as a positional arg or via stdin.
    # Combine system + user into one prompt and pass via stdin to avoid
    # the Windows CreateProcess ~32K command-line ceiling on large compiles.
    combined = prompt
    if system_prompt:
        combined = f"<system>{system_prompt}</system>\n\n{prompt}"

    try:
        command = [
            claude_bin,
            "-p",  # print mode (non-interactive)
            "--output-format",
            "text",
        ]
        model = descriptor.model
        if model:
            command.extend(["--model", model])
        result = subprocess.run(
            command,
            input=combined,
            capture_output=True,
            timeout=_timeout_s(),
            check=False,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        return result.stdout or ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


# ---------------------------------------------------------------------------
# Backend 4: OpenAI-compatible HTTP API (optional, paid)
# ---------------------------------------------------------------------------


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
    base_url = descriptor._endpoint
    model = descriptor.model
    max_tokens = int(descriptor.inference_settings["max_tokens"])
    temperature = int(descriptor.inference_settings["temperature_milli"]) / 1000
    url = f"{base_url.rstrip('/')}/chat/completions"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    payload: dict[str, object] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
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
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_timeout_s()) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return BackendResponse(
        data["choices"][0]["message"]["content"],
        _parse_http_usage(data),
    )


# ---------------------------------------------------------------------------
# Backend 5: Ollama HTTP API (optional, local, free, offline)
# ---------------------------------------------------------------------------


def _call_ollama(
    descriptor: ProviderDescriptor,
    prompt: str,
    system_prompt: str,
    schema: Mapping[str, object] | None = None,
) -> str | BackendResponse:
    if descriptor._endpoint is None:
        raise ValueError("Ollama endpoint was not resolved in the provider descriptor")
    base_url = descriptor._endpoint
    model = descriptor.model
    max_tokens = int(descriptor.inference_settings["max_tokens"])
    temperature = int(descriptor.inference_settings["temperature_milli"]) / 1000
    url = f"{base_url.rstrip('/')}/chat/completions"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    payload: dict[str, object] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
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
    body = json.dumps(payload).encode("utf-8")
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


def _cli() -> int:
    """Quick CLI: `python llm_client.py "your prompt"` to test backends."""
    if len(sys.argv) < 2:
        print("Usage: python llm_client.py \"<prompt>\"", file=sys.stderr)
        print("\nBackend availability:", file=sys.stderr)
        for name in _PROBES:
            try:
                alive = probe_candidate(provider_candidates(name)[0])
            except Exception:  # noqa: BLE001
                alive = False
            print(f"  {name}: {'ALIVE' if alive else 'not available'}", file=sys.stderr)
        return 1
    prompt = sys.argv[1]
    system = sys.argv[2] if len(sys.argv) > 2 else ""
    print("--- backend availability ---", file=sys.stderr)
    for name in _PROBES:
        try:
            alive = probe_candidate(provider_candidates(name)[0])
        except Exception:  # noqa: BLE001
            alive = False
        print(f"  {name}: {'ALIVE' if alive else 'not available'}", file=sys.stderr)
    print("--- calling first alive backend ---", file=sys.stderr)
    response = call_llm(prompt, system)
    print(response)
    return 0 if response else 2


if __name__ == "__main__":
    raise SystemExit(_cli())
