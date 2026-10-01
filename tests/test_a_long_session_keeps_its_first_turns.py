"""A long transcript's head and tail are its turns, not service records (audit 2026-09-26 C-5).

docs/research/2026-09-26-a-long-session-keeps-its-first-turns.md
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import integration_adapter
import pytest
from session_evidence import is_service_record, render_transcript

LIMIT = 64 * 1024


def _line(entry: dict) -> str:
    return json.dumps(entry) + "\n"


def _turn(role: str, text: str) -> str:
    return _line({"type": role, "message": {"content": [{"type": "text", "text": text}]}})


def _snapshots(count: int) -> str:
    snapshot = {"type": "file-history-snapshot", "snapshot": {"files": "x" * 900}}
    return _line(snapshot) * count


def _transcript(tmp_path: Path) -> Path:
    body = (
        _snapshots(200)
        + _turn("user", "FIRST QUESTION")
        + "".join(_turn("assistant", f"middle {index} " + "y" * 900) for index in range(400))
        + _turn("assistant", "LAST ANSWER")
        + _snapshots(200)
    )
    path = tmp_path / "session.jsonl"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_first_and_last_turns_survive_a_head_and_tail_of_service_records(tmp_path) -> None:
    text = integration_adapter._capture_transcript_text(_transcript(tmp_path), LIMIT)
    rendered = render_transcript(text)

    assert ("FIRST QUESTION" in rendered, "LAST ANSWER" in rendered, len(text) <= LIMIT + 512) == (
        True,
        True,
        True,
    )


def test_a_plain_text_line_is_not_a_service_record() -> None:
    kinds = [
        is_service_record(_snapshots(1).strip()),
        is_service_record(_turn("user", "hi").strip()),
        is_service_record("a plain log line"),
    ]

    assert kinds == [True, False, False]


@pytest.mark.parametrize("large", [False, True])
def test_capture_keeps_a_verified_prefix_when_the_host_appends(tmp_path, monkeypatch, large):
    path = _transcript(tmp_path)
    if not large:
        path.write_text(_turn("user", "FIRST QUESTION") + _turn("assistant", "LAST ANSWER"))
    expected = integration_adapter._capture_transcript_text(path, LIMIT)
    original = os.read
    appended = False

    def read_then_append(descriptor, count):
        nonlocal appended
        data = original(descriptor, count)
        if not appended:
            appended = True
            with path.open("ab") as stream:
                stream.write(_turn("assistant", "LATER TURN").encode())
        return data

    monkeypatch.setattr(os, "read", read_then_append)
    actual = integration_adapter._capture_transcript_text(path, LIMIT)

    assert appended
    assert actual == expected
    assert "LATER TURN" not in actual


@pytest.mark.parametrize("large", [False, True])
def test_capture_refuses_rewritten_bytes_even_when_the_file_grows(tmp_path, monkeypatch, large):
    path = _transcript(tmp_path)
    if not large:
        path.write_text(_turn("user", "FIRST QUESTION"))
    original = os.read
    rewritten = False

    def read_then_rewrite(descriptor, count):
        nonlocal rewritten
        data = original(descriptor, count)
        if not rewritten:
            rewritten = True
            raw = path.read_bytes()
            path.write_bytes(b"X" + raw[1:] + b"\nLATER\n")
        return data

    monkeypatch.setattr(os, "read", read_then_rewrite)
    with pytest.raises((ValueError, PermissionError), match="changed"):
        integration_adapter._capture_transcript_text(path, LIMIT)


def _replace_transcript(path: Path, raw: bytes) -> None:
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(raw)
    replacement.replace(path)


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("mutation", [
    "truncate", "rewrite",
    pytest.param("replace", marks=pytest.mark.skipif(
        os.name == "nt", reason="Windows denies rename while the capture descriptor is open",
    )),
])
def test_capture_rejects_other_concurrent_changes(tmp_path, monkeypatch, large, mutation):
    path = _transcript(tmp_path)
    if not large:
        path.write_text(_turn("user", "FIRST QUESTION"))
    raw = path.read_bytes()
    changes = {
        "truncate": lambda: path.write_bytes(b""),
        "rewrite": lambda: path.write_bytes(b"X" + raw[1:]),
        "replace": lambda: _replace_transcript(path, raw),
    }
    original = os.read
    changed = False

    def read_then_change(descriptor, count):
        nonlocal changed
        data = original(descriptor, count)
        if not changed:
            changed = True
            changes[mutation]()
        return data

    monkeypatch.setattr(os, "read", read_then_change)
    with pytest.raises((ValueError, PermissionError), match="changed|replaced"):
        integration_adapter._capture_transcript_text(path, LIMIT)


@pytest.mark.skipif(os.name != "nt", reason="Native Windows handle sharing")
@pytest.mark.parametrize("large", [False, True])
def test_windows_blocks_replacement_until_capture_closes_its_descriptor(
    tmp_path, monkeypatch, large,
):
    path = _transcript(tmp_path)
    if not large:
        path.write_text(_turn("user", "FIRST QUESTION"))
    raw, before = path.read_bytes(), path.stat()
    expected = integration_adapter._capture_transcript_text(path, LIMIT)
    replacement_bytes = b"X" + raw[1:]
    original = os.read
    attempted = False

    def read_then_try_replacement(descriptor, count):
        nonlocal attempted
        data = original(descriptor, count)
        if not attempted:
            attempted = True
            with pytest.raises(PermissionError) as refused:
                _replace_transcript(path, replacement_bytes)
            assert refused.value.winerror in {5, 32}
        return data

    monkeypatch.setattr(os, "read", read_then_try_replacement)
    assert integration_adapter._capture_transcript_text(path, LIMIT) == expected
    assert attempted
    assert os.path.samestat(before, path.stat())
    assert path.read_bytes() == raw
    replacement = path.with_suffix(".replacement")
    assert replacement.read_bytes() == replacement_bytes
    replacement.replace(path)
    assert path.read_bytes() == replacement_bytes


def test_capture_rejects_growth_during_verification(tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl"
    path.write_text(_turn("user", "FIRST QUESTION"))
    original = os.read

    def read_then_append(descriptor, count):
        data = original(descriptor, count)
        with path.open("ab") as stream:
            stream.write(_turn("assistant", "LATER TURN").encode())
        return data

    monkeypatch.setattr(os, "read", read_then_append)
    with pytest.raises(ValueError, match="changed"):
        integration_adapter._capture_transcript_text(path, LIMIT)


@pytest.mark.parametrize("event_type", ["pre_compact", "session_end"])
def test_appending_host_transcript_reaches_durable_capture(tmp_path, monkeypatch, event_type):
    from session_evidence import evidence_text

    from tests.adopted_capture_vault import (
        adopted_capture_vault,
        host_transcript,
        published_intents,
    )

    state_root, project = adopted_capture_vault(tmp_path, monkeypatch, integration_adapter)
    path = host_transcript(state_root, "growing.jsonl", _turn("user", "FIRST QUESTION"))
    original = integration_adapter._read_transcript_edge
    appended = False

    def read_then_append(descriptor, offset, size):
        nonlocal appended
        data = original(descriptor, offset, size)
        if not appended:
            appended = True
            with path.open("ab") as stream:
                stream.write(_turn("assistant", "LATER TURN").encode())
        return data

    monkeypatch.setattr(integration_adapter, "_read_transcript_edge", read_then_append)
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda _args: 1)
    event = integration_adapter.normalize_event(
        "claude", event_type,
        {"session_id": "growing", "cwd": str(project), "transcript_path": str(path)},
    )
    result = integration_adapter.ingest_event(event)
    records = [json.loads(item.read_text()) for item in published_intents(state_root)]
    assert len(result["capture_intent_ids"]) == len(records) == 1
    captured = render_transcript(evidence_text(records[0]["evidence"]))
    assert "FIRST QUESTION" in captured
    assert "LATER TURN" not in captured
