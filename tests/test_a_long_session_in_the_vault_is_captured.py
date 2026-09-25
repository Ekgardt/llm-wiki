"""A long session in the vault is captured, and each capture keeps its own window.

The owner's sessions in the vault had no prompt capture, a second capture of a
session replaced the first record, a failed hook delegate left no trace, and the
daily log said "When When". See
docs/research/2026-09-24-a-long-session-in-the-vault-is-captured.md.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import episode_consolidation
import integration_adapter
import session_evidence
import user_prompt_capture

FIELDS = {"session": "abc-session", "captured_at": "2026-09-24T10:00:00+00:00"}


def test_a_prompt_is_skipped_for_its_length_only() -> None:
    long_prompt = "x" * (user_prompt_capture.MIN_PROMPT_CHARS + 1)

    assert (user_prompt_capture._should_skip("x"), user_prompt_capture._should_skip(long_prompt)) == (True, False)


def test_a_second_window_of_a_session_is_kept_beside_the_first(tmp_path: Path) -> None:
    first = session_evidence._free_relative_path(tmp_path, FIELDS, b"window one")
    (tmp_path / first).parent.mkdir(parents=True)
    (tmp_path / first).write_bytes(b"window one")

    again = session_evidence._free_relative_path(tmp_path, FIELDS, b"window one")
    second = session_evidence._free_relative_path(tmp_path, FIELDS, b"window two")

    assert again == first
    assert second != first and second.startswith(first[: -len(".md")] + "@")


def test_a_failed_delegate_is_recorded(monkeypatch) -> None:
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "capture_diagnostics.record_capture_failure",
        lambda kind, reason, **_fields: recorded.append((kind, reason)),
    )
    failed = subprocess.CompletedProcess([], 1, stdout="", stderr="Traceback\nImportError: no memory_state\n")
    passed = subprocess.CompletedProcess([], 0, stdout="", stderr="")

    integration_adapter._record_failed_delegate("user_prompt_capture.py", failed)
    integration_adapter._record_failed_delegate("user_prompt_capture.py", passed)

    assert recorded == [("delegate_user_prompt_capture", "exit 1: ImportError: no memory_state")]


def test_a_rule_says_when_once() -> None:
    assert episode_consolidation._situation("When planning a release") == "planning a release"
    assert episode_consolidation._situation("a release is planned") == "a release is planned"
