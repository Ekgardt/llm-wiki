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

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

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
    # Test/e2e backend: return canned response without network or CLI.
    if forced == "fake":
        return os.environ.get(
            "MEMORY_LLM_FAKE_RESPONSE",
            '{"operations": [], "audit": {"verified": 0, "dedup": 0, "stubs": 0, "contradictions": 0, "rejected": 0}}\nCOMPILE_AUDIT: verified 0 evidence citations; 0 dedup checks performed; 0 stubs skipped; 0 contradictions handled; 0 pages rejected as below-threshold',
        ).strip()

    candidates = _candidate_order(forced)

    for name in candidates:
        try:
            probe = _PROBES.get(name)
            if probe and not probe():
                continue
            caller = _BACKENDS.get(name)
            if not caller:
                continue
            result = caller(prompt, system_prompt, max_tokens)
            if result and result.strip():
                return result.strip()
        except Exception as e:  # noqa: BLE001
            print(f"llm_client: {name} backend failed: {type(e).__name__}: {e}", file=sys.stderr)
            continue

    # No backend available — return None. Callers handle this gracefully
    # (compile skips, flush treats as FLUSH_OK, query returns error string).
    # The queue is available as an explicit API for deferred execution.
    return None


def _candidate_order(forced: str) -> list[str]:
    """Order in which to try backends.

    When ``forced`` is set to a known backend, ONLY that backend is tried —
    a strict override. If it fails, the call returns None rather than
    silently falling through to another provider. When ``forced`` is empty
    or unknown, the full default order is used (auto-detection).
    """
    defaults = ["opencode", "codex", "claude", "openai", "ollama"]
    if forced and forced in defaults:
        return [forced]
    return defaults


# ---------------------------------------------------------------------------
# Liveness probes (cheap, before attempting real call)
# ---------------------------------------------------------------------------


def _probe_opencode() -> bool:
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


def _probe_codex() -> bool:
    return _find_codex_binary() is not None


def _probe_claude() -> bool:
    return shutil.which("claude") is not None


def _probe_openai() -> bool:
    return bool(
        os.environ.get("MEMORY_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    )


def _probe_ollama() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=0.5):
            return True
    except OSError:
        return False


_PROBES = {
    "opencode": _probe_opencode,
    "codex": _probe_codex,
    "claude": _probe_claude,
    "openai": _probe_openai,
    "ollama": _probe_ollama,
}


def _timeout_s() -> int:
    return int(os.environ.get("MEMORY_LLM_TIMEOUT_S", "90"))


# ---------------------------------------------------------------------------
# Backend 1: OpenCode server (HTTP API) — uses your OpenCode subscription
# ---------------------------------------------------------------------------


def _call_opencode(prompt: str, system_prompt: str, max_tokens: int) -> str:
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
        parts = parts_root.get("parts", []) or []
        return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
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


def _call_codex(prompt: str, system_prompt: str, max_tokens: int) -> str:
    """Call `codex exec` and return the model's final message."""
    reasoning = os.environ.get("MEMORY_CODEX_REASONING", "low")
    model = os.environ.get("MEMORY_CODEX_MODEL")
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


def _call_claude(prompt: str, system_prompt: str, max_tokens: int) -> str:
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
        result = subprocess.run(
            [
                claude_bin,
                "-p",  # print mode (non-interactive)
                "--output-format", "text",
            ],
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


def _call_openai(prompt: str, system_prompt: str, max_tokens: int) -> str:
    api_key = os.environ.get("MEMORY_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return ""
    base_url = os.environ.get("MEMORY_LLM_BASE_URL", "https://api.openai.com/v1")
    model = os.environ.get("MEMORY_LLM_MODEL", "gpt-4o-mini")
    url = f"{base_url.rstrip('/')}/chat/completions"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
    ).encode("utf-8")
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
    return data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Backend 5: Ollama HTTP API (optional, local, free, offline)
# ---------------------------------------------------------------------------


def _call_ollama(prompt: str, system_prompt: str, max_tokens: int) -> str:
    base_url = os.environ.get("MEMORY_LLM_BASE_URL", "http://localhost:11434/v1")
    model = os.environ.get("MEMORY_LLM_MODEL", "qwen3:0.6b")
    url = f"{base_url.rstrip('/')}/chat/completions"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_timeout_s()) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


# Backend registry.
_BACKENDS = {
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
        for name, probe in _PROBES.items():
            try:
                alive = bool(probe())
            except Exception:  # noqa: BLE001
                alive = False
            print(f"  {name}: {'ALIVE' if alive else 'not available'}", file=sys.stderr)
        return 1
    prompt = sys.argv[1]
    system = sys.argv[2] if len(sys.argv) > 2 else ""
    print("--- backend availability ---", file=sys.stderr)
    for name, probe in _PROBES.items():
        try:
            alive = bool(probe())
        except Exception:  # noqa: BLE001
            alive = False
        print(f"  {name}: {'ALIVE' if alive else 'not available'}", file=sys.stderr)
    print("--- calling first alive backend ---", file=sys.stderr)
    response = call_llm(prompt, system)
    print(response)
    return 0 if response else 2


if __name__ == "__main__":
    raise SystemExit(_cli())

