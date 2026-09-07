"""The classifier must read what people said, not the JSON that carries it.

Measured on this vault on 2026-09-06: a session's capture evidence is 143 KB
to 942 KB of host JSONL, and the conversation inside it renders to 2 KB to
32 KB. The classifier read the last 60 000 characters of the raw form — about
six per cent of the bytes, taken from the end — and on the intents inspected
that window was file-backup manifests and token-cost accounting from edge to
edge. `flush_tier_counts` was `{"ok": 65}`: sixty-five sessions in a row said
to hold nothing worth saving, each verdict correct about the bytes it saw.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import flush_memory  # noqa: E402

NOISE = {
    "type": "file-history-snapshot",
    "snapshot": {"trackedFileBackups": {f"file-{n}.py": "x" * 200 for n in range(60)}},
}
SAID = {
    "type": "user",
    "message": {"role": "user", "content": [{"type": "text", "text": "We chose lzma."}]},
}


def _transcript(lines):
    return "\n".join(json.dumps(line) for line in lines)


def test_the_conversation_survives_a_wall_of_bookkeeping():
    raw = _transcript([NOISE] * 40 + [SAID] + [NOISE] * 40)

    readable = flush_memory._readable_evidence(raw)

    assert "We chose lzma." in readable
    assert "trackedFileBackups" not in readable
    assert len(readable) < len(raw)


def test_the_window_would_have_missed_it():
    """The same conversation, under the old rule, is outside the window."""
    raw = _transcript([SAID] + [NOISE] * 400)

    assert len(raw) > flush_memory.MAX_TRANSCRIPT_CHARS
    assert "We chose lzma." not in flush_memory._bounded_classifier_evidence(raw)
    assert "We chose lzma." in flush_memory._readable_evidence(raw)


def test_evidence_arriving_as_a_capture_intent_list_is_rendered():
    raw = _transcript([SAID, NOISE])
    evidence = [{"parts": [{"text": raw}], "role": "transcript"}]

    prompt = flush_memory._capture_prompt(
        {"event": "session_end", "evidence": evidence}
    )

    assert "We chose lzma." in prompt
    assert "trackedFileBackups" not in prompt


def test_text_that_is_not_a_transcript_is_kept_as_it_stands():
    assert flush_memory._readable_evidence("just a note") == "just a note"
