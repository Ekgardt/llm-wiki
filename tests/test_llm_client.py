from __future__ import annotations

import llm_client


def test_opencode_sdk_never_selects_synchronous_backend(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "opencode-sdk")
    called = []
    monkeypatch.setattr(llm_client, "_BACKENDS", {"codex": lambda *_: called.append(1)})

    assert llm_client.call_llm("prompt") is None
    assert called == []


def test_auto_mode_rejects_not_logged_in_and_falls_through(monkeypatch):
    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
    monkeypatch.setattr(llm_client, "_candidate_order", lambda _: ["claude", "opencode"])
    monkeypatch.setitem(llm_client._PROBES, "claude", lambda: True)
    monkeypatch.setitem(llm_client._PROBES, "opencode", lambda: True)
    monkeypatch.setitem(
        llm_client._BACKENDS,
        "claude",
        lambda *_: "Not logged in · Please run /login",
    )
    monkeypatch.setitem(llm_client._BACKENDS, "opencode", lambda *_: "valid response")

    assert llm_client.call_llm("prompt") == "valid response"


def test_auto_mode_rejects_model_compatibility_error(monkeypatch):
    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
    monkeypatch.setattr(llm_client, "_candidate_order", lambda _: ["codex", "opencode"])
    monkeypatch.setitem(llm_client._PROBES, "codex", lambda: True)
    monkeypatch.setitem(llm_client._PROBES, "opencode", lambda: True)
    monkeypatch.setitem(
        llm_client._BACKENDS,
        "codex",
        lambda *_: "The model requires a newer version of Codex",
    )
    monkeypatch.setitem(llm_client._BACKENDS, "opencode", lambda *_: "fallback")

    assert llm_client.call_llm("prompt") == "fallback"


def test_forced_provider_does_not_fall_through(monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "claude")
    monkeypatch.setitem(llm_client._PROBES, "claude", lambda: True)
    monkeypatch.setitem(
        llm_client._BACKENDS,
        "claude",
        lambda *_: "Not logged in · Please run /login",
    )

    assert llm_client.call_llm("prompt") is None
