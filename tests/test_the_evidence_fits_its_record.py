"""A transcript that grows inside JSON is still captured, and a cut record says it was cut.

See `docs/research/2026-09-17-the-evidence-fits-its-record-and-says-what-it-dropped.md`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from tests.adopted_capture_vault import (  # noqa: E402
    adopted_capture_vault,
    host_transcript,
    published_intents,
)

# Quotes, backslashes and line breaks: every one of them doubles inside a JSON string.
ESCAPE_HEAVY = 'print("a\\\\b")\n' * 60


def _turn(index: int) -> str:
    entry = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": f"turn {index}\n{ESCAPE_HEAVY}"}]},
    }
    return json.dumps(entry) + "\n"


def _escape_heavy_transcript(size: int) -> str:
    turns: list[str] = []
    total = 0
    while total < size:
        turns.append(_turn(len(turns)))
        total += len(turns[-1])
    return "".join(turns)


def _publish(monkeypatch, tmp_path, size: int) -> tuple[Path, Path]:
    """(state root, transcript) after one pre-compact capture of a transcript this size."""
    import integration_adapter

    state_root, project = adopted_capture_vault(tmp_path, monkeypatch, integration_adapter)
    transcript = host_transcript(state_root, "long.jsonl", _escape_heavy_transcript(size))
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda _args: 1)
    payload = {"session_id": "long", "cwd": str(project), "transcript_path": str(transcript)}
    integration_adapter.capture_running_session("claude", payload)
    return state_root, transcript


def test_a_transcript_under_the_bound_that_outgrows_the_record_is_still_captured(
    monkeypatch, tmp_path
):
    import integration_adapter

    state_root, transcript = _publish(monkeypatch, tmp_path, 880 * 1024)

    sizes = [path.stat().st_size for path in published_intents(state_root)]
    assert transcript.stat().st_size <= integration_adapter.MAX_CAPTURE_EVIDENCE_BYTES
    assert [size <= integration_adapter.MAX_CAPTURE_INTENT_BYTES for size in sizes] == [True]


def test_the_stored_record_of_a_cut_session_says_it_was_cut(monkeypatch, tmp_path):
    from session_evidence import evidence_text, render_transcript

    state_root, _transcript = _publish(monkeypatch, tmp_path, 3 * 1024 * 1024)

    record = json.loads(published_intents(state_root)[0].read_text(encoding="utf-8"))
    rendered = render_transcript(evidence_text(record["evidence"]))
    assert "bytes of this transcript were not captured" in rendered
    assert ("**assistant:** turn 0" in rendered, '{"type"' in rendered) == (True, False)


def test_a_plain_text_transcript_keeps_its_words_and_its_gap():
    from session_evidence import capture_gap_line, render_transcript

    text = f"first words\n{capture_gap_line(42)}\nlast words\n"

    assert render_transcript(text) == (
        "first words\n"
        "_(42 bytes of this transcript were not captured; "
        "the durable record keeps the beginning and the end.)_\n"
        "last words"
    )
