"""The protected text counted before dispatch is the exact Codex stdin."""
from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import llm_client
import pytest

from tests.test_llm_descriptors import _write_dlp_policy


@pytest.fixture
def codex_executable_alias(monkeypatch, tmp_path):
    """Old mocked CLI names bind a real test file while retaining command assertions."""
    executable = tmp_path / "mocked-cli-file"
    executable.write_bytes(b"owned mocked CLI fixture")
    bind = llm_client._bind_codex_executable

    def binding(path):
        return replace(bind(str(executable)), path=path)

    monkeypatch.setattr(llm_client, "_bind_codex_executable", binding)


def _service(monkeypatch, tmp_path, model="proof-model"):
    monkeypatch.setenv("MEMORY_CODEX_MODEL", model)
    executable = tmp_path / "codex"
    executable.write_bytes(b"owned executable fixture")
    selected = {"path": str(executable), "calls": 0}
    captured = []

    def locate():
        selected["calls"] += 1
        return selected["path"]

    def run(command, **kwargs):
        captured.append((command, kwargs["stdin"].read().decode("utf-8")))
        Path(command[command.index("--output-last-message") + 1]).write_text("answer")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(llm_client, "_find_codex_binary", locate)
    monkeypatch.setattr(llm_client, "_run_cli", run)
    return executable, selected, captured


def _call(counter):
    descriptor = llm_client.provider_candidates("codex", max_tokens=4000)[0]
    return llm_client.call_candidate(
        descriptor, '{"user_prompt":"строка e\u0301\\n全体"}', "Only evidence",
        max_tokens=4000, schema={"type": "object"}, available=True,
        token_adapters={"proof-model": counter},
    )


def test_counter_sees_exact_protected_schema_system_and_unicode_stdin(monkeypatch, tmp_path):
    _, _, captured = _service(monkeypatch, tmp_path)
    policy = tmp_path / "policy.json"
    _write_dlp_policy(policy, literals=("Only evidence",))
    monkeypatch.setenv("LLM_WIKI_DLP_POLICY", str(policy))
    counted = []

    def count(text):
        counted.append(text)
        return len(text.encode("utf-8"))

    result = _call(count)
    command, stdin = captured[0]
    assert counted == [stdin]
    assert llm_client.TASK_FRAME in stdin and "Only evidence" not in stdin
    assert "Output only JSON matching this schema" in stdin and "e\u0301" in stdin
    assert result.text == "answer" and result.input_token_count.tokens == len(stdin.encode())
    assert command[command.index("-m") + 1] == result.descriptor.model == "proof-model"
    assert result.descriptor.capabilities["max_tokens_enforced"] is False
    assert result.descriptor.inference_settings["max_tokens"] == "backend_default"


def test_selected_executable_is_bound_before_counting_and_not_resolved_again(monkeypatch, tmp_path):
    first, selected, captured = _service(monkeypatch, tmp_path)
    replacement = tmp_path / "another-codex"
    replacement.write_bytes(b"different executable fixture")

    def count(text):
        selected["path"] = str(replacement)
        return len(text.encode())

    result = _call(count)
    assert result.text == "answer"
    assert captured[0][0][0] == str(first)
    assert selected["calls"] == 1


@pytest.mark.parametrize("change", ["content", "replacement", "disappearance"])
def test_binary_drift_after_counting_refuses_before_dispatch(monkeypatch, tmp_path, change):
    executable, _, captured = _service(monkeypatch, tmp_path)

    def count(text):
        _change_executable(executable, change)
        return len(text.encode())

    result = _call(count)
    assert result.text is None and result.failure_class == "provider_error"
    assert captured == []


def _change_executable(executable, change):
    if change == "disappearance":
        executable.unlink()
        return
    if change == "replacement":
        replacement = executable.with_suffix(".replacement")
        replacement.write_bytes(executable.read_bytes())
        replacement.replace(executable)
        return
    executable.write_bytes(b"changed executable fixture")


def test_implicit_model_remains_unknown_and_uses_no_model_override(monkeypatch, tmp_path):
    _, _, captured = _service(monkeypatch, tmp_path)
    monkeypatch.delenv("MEMORY_CODEX_MODEL")
    descriptor = llm_client.provider_candidates("codex")[0]
    result = llm_client.call_candidate(descriptor, "question", "", available=True)
    assert result.descriptor is descriptor and descriptor.model is None
    assert "-m" not in captured[0][0] and result.text == "answer"
    assert result.input_token_count.tokens == len(captured[0][1].encode("utf-8"))


def test_symlink_retarget_after_counting_refuses_before_dispatch(monkeypatch, tmp_path):
    executable, selected, captured = _service(monkeypatch, tmp_path)
    link = tmp_path / "selected-link"
    _symlink_or_skip(link, executable)
    selected["path"] = str(link)
    replacement = tmp_path / "other-target"
    replacement.write_bytes(executable.read_bytes())

    def count(text):
        link.unlink()
        link.symlink_to(replacement)
        return len(text.encode())

    result = _call(count)
    assert result.text is None and result.failure_class == "provider_error"
    assert captured == []


def _symlink_or_skip(link, target):
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlink creation unsupported: {type(error).__name__}")


def test_large_native_json_is_counted_and_forwarded_whole(monkeypatch, tmp_path):
    _, _, captured = _service(monkeypatch, tmp_path)
    prompt = '{"user_prompt":"' + "完整 e\u0301\\n" * 3000 + '"}'
    counted = []

    def count(text):
        counted.append(text)
        return len(text.encode())

    descriptor = llm_client.provider_candidates("codex")[0]
    result = llm_client.call_candidate(
        descriptor, prompt, "evidence", available=True,
        token_adapters={"proof-model": count},
    )
    assert counted == [captured[0][1]] and prompt in captured[0][1]
    assert result.input_token_count.tokens == len(captured[0][1].encode())


def test_relative_finder_path_is_pinned_before_neutral_cwd_changes(monkeypatch, tmp_path):
    executable, selected, captured = _service(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    selected["path"] = executable.name
    result = _call(len)
    assert result.text == "answer"
    assert captured[0][0][0] == str(executable)
