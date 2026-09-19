"""What shapes a provider call at install time reaches the unattended runs too.

Finding M-D5 of the third audit. See
`docs/research/2026-09-18-the-installed-choice-of-provider-travels-whole.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import integration_hook_config  # noqa: E402

LOCAL_ONLY = {
    "MEMORY_LLM_PROVIDER": "ollama",
    "OLLAMA_NO_CLOUD": "1",
    "MEMORY_LLM_BASE_URL": "http://127.0.0.1:11434/v1",
    "MEMORY_LLM_MODEL": "qwen3:8b",
}


def _install_environment(monkeypatch: pytest.MonkeyPatch, values: dict) -> dict:
    for name in integration_hook_config.PROVIDER_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return integration_hook_config.provider_environment()


def test_a_local_only_install_persists_what_makes_it_local_only(monkeypatch) -> None:
    """Only the provider travelled, so scheduled runs left the loopback rule behind."""
    persisted = _install_environment(monkeypatch, LOCAL_ONLY)

    assert persisted == LOCAL_ONLY


def test_the_codex_model_and_reasoning_travel_too(monkeypatch) -> None:
    chosen = {
        "MEMORY_LLM_PROVIDER": "codex",
        "MEMORY_CODEX_MODEL": "gpt-5.6-sol",
        "MEMORY_CODEX_REASONING": "medium",
    }

    assert _install_environment(monkeypatch, chosen) == chosen


def test_a_key_is_never_written_into_a_unit_or_a_settings_file(monkeypatch) -> None:
    """A secret on disk is a secret in every backup of the vault."""
    monkeypatch.setenv("MEMORY_LLM_API_KEY", "sk-should-not-travel")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-travel-either")

    persisted = _install_environment(
        monkeypatch, {"MEMORY_LLM_PROVIDER": "openai", "MEMORY_LLM_MODEL": "gpt-4o-mini"}
    )

    assert "MEMORY_LLM_API_KEY" not in integration_hook_config.PROVIDER_ENV_KEYS
    assert set(persisted) == {"MEMORY_LLM_PROVIDER", "MEMORY_LLM_MODEL"}


def test_the_test_provider_still_persists_nothing(monkeypatch) -> None:
    persisted = _install_environment(
        monkeypatch, {"MEMORY_LLM_PROVIDER": "fake", "MEMORY_LLM_MODEL": "anything"}
    )

    assert persisted == {}


def test_a_scheduler_unit_carries_the_local_only_switch(tmp_path, monkeypatch) -> None:
    import install_control

    _install_environment(monkeypatch, LOCAL_ONLY)

    unit = install_control._systemd_service(
        tmp_path, tmp_path / "state", tmp_path / "uv", "nightly"
    ).decode()

    assert 'Environment="OLLAMA_NO_CLOUD=1"' in unit
    assert 'Environment="MEMORY_LLM_BASE_URL=http://127.0.0.1:11434/v1"' in unit
