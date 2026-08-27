"""Every captured session leaves a readable, searchable record of itself.

The classifier used to decide whether a session was worth keeping at all, and on
this vault's own sessions it kept one in forty. Retention no longer depends on
that judgement.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import session_evidence  # noqa: E402


def _line(kind: str, content) -> str:
    return json.dumps({"type": kind, "message": {"role": kind, "content": content}})


TRANSCRIPT = "\n".join(
    [
        _line("user", "why systemd and not cron?"),
        _line(
            "assistant",
            [
                {"type": "text", "text": "systemd user timers survive a reboot"},
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "systemctl --user list-timers"},
                },
            ],
        ),
        _line("user", [{"type": "tool_result", "content": "NEXT LEFT LAST ..."}]),
        json.dumps({"type": "summary", "summary": "noise"}),
    ]
)


def test_the_conversation_is_kept() -> None:
    rendered = session_evidence.render_transcript(TRANSCRIPT)

    assert "**user:** why systemd and not cron?" in rendered
    assert "**assistant:** systemd user timers survive a reboot" in rendered
    assert "- tool `Bash`: systemctl --user list-timers" in rendered


def test_the_tool_output_is_not_kept() -> None:
    """Tool output is the noise that drowns the signal; the call itself is a line."""
    rendered = session_evidence.render_transcript(TRANSCRIPT)

    assert "NEXT LEFT LAST" not in rendered
    assert "noise" not in rendered


def test_a_transcript_that_is_not_jsonl_is_kept_verbatim() -> None:
    rendered = session_evidence.render_transcript("plain notes\nsecond line")

    assert rendered == "plain notes\nsecond line"


def test_the_document_carries_frontmatter_and_names_the_session() -> None:
    document = session_evidence.render_session_document(
        {"session": "abc123", "project": "llm-wiki", "captured_at": "2026-08-23T10:00:00Z"},
        TRANSCRIPT,
    )

    assert document.startswith("---\ntype: raw-source\n")
    assert "session: abc123" in document
    assert "project: llm-wiki" in document
    assert "# Session abc123" in document


def test_an_oversized_record_is_bounded_and_says_so(monkeypatch) -> None:
    monkeypatch.setattr(session_evidence, "MAX_EVIDENCE_BYTES", 400)

    document = session_evidence.render_session_document(
        {"session": "big"}, _line("user", "x" * 5000)
    )

    assert len(document.encode("utf-8")) < 800
    assert "record truncated" in document


def test_the_path_is_by_day_and_session(tmp_path: Path) -> None:
    relative = session_evidence.evidence_relative_path("2026-08-23", "abc/../123")

    assert relative == "knowledge/raw/sessions/2026-08-23/abc-123.md"
    assert ".." not in Path(relative).parts


def test_writing_the_record_goes_through_the_transaction(tmp_path: Path, monkeypatch) -> None:
    written: dict[Path, bytes] = {}
    monkeypatch.setattr(
        "markdown_transaction.mutate_knowledge",
        lambda operation_id, changes, **kwargs: written.update(changes),
    )

    path = session_evidence.write_session_evidence(
        tmp_path,
        {"session": "abc123", "captured_at": "2026-08-23T10:00:00Z"},
        TRANSCRIPT,
    )

    assert path == tmp_path / "knowledge/raw/sessions/2026-08-23/abc123.md"
    assert written and b"why systemd and not cron?" in next(iter(written.values()))


def test_a_session_with_nothing_in_it_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "markdown_transaction.mutate_knowledge",
        lambda operation_id, changes, **kwargs: calls.append(changes),
    )

    path = session_evidence.write_session_evidence(tmp_path, {"session": "empty"}, "   ")

    assert path is None
    assert calls == []


def test_a_failed_write_never_breaks_capture(tmp_path: Path, monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise RuntimeError("transaction refused")

    monkeypatch.setattr("markdown_transaction.mutate_knowledge", explode)

    assert session_evidence.write_session_evidence(tmp_path, {"session": "s"}, TRANSCRIPT) is None


def test_intent_evidence_text_is_recovered() -> None:
    evidence = [{"role": "transcript", "parts": [{"type": "text", "text": "hello"}]}]

    assert session_evidence.evidence_text(evidence) == "hello"


@pytest.mark.parametrize("value", ["", None, 5])
def test_broken_evidence_is_survivable(value) -> None:
    assert session_evidence.evidence_text([value]) == ""


def test_the_record_is_written_even_when_the_classifier_keeps_nothing(monkeypatch):
    """The whole point: retention must not depend on the tier.

    Measured on this vault's own sessions, the classifier answered "nothing worth
    keeping" 39 times out of 40.
    """
    import argparse

    import flush_memory

    transcripts = Path(flush_memory.STATE_ROOT) / "cache" / "transient-transcripts"
    transcripts.mkdir(parents=True, exist_ok=True)
    transcript = transcripts / "kept.jsonl"
    transcript.write_text(TRANSCRIPT, encoding="utf-8")
    written = []
    monkeypatch.setattr(flush_memory, "should_skip", lambda *_args: False)
    monkeypatch.setattr(flush_memory, "_flush_summary", lambda _args: "FLUSH_OK")
    monkeypatch.setattr(flush_memory, "_record_empty_flush", lambda _args: None)
    monkeypatch.setattr(
        "session_evidence.write_session_evidence",
        lambda vault, fields, text: written.append((fields, text)) or Path("written.md"),
    )
    args = argparse.Namespace(
        session_id="session-9",
        event="session-end",
        transcript=str(transcript),
        agent="claude",
        source_event_id="event-9",
    )

    assert flush_memory._run_flush(args) == 0
    assert written, "the session was dropped because the classifier said ok"
    assert written[0][0]["session"] == "session-9"


def test_the_capture_worker_writes_the_record_before_classifying(monkeypatch):
    import flush_memory

    written = []
    monkeypatch.setattr(
        "session_evidence.write_session_evidence",
        lambda vault, fields, text, **_owned: written.append((fields, text)),
    )
    record = {
        "session": "session-10",
        "project_slug": "llm-wiki",
        "host": "claude",
        "event": "session_end",
        "source_event_id": "event-10",
        "evidence": [{"role": "transcript", "parts": [{"type": "text", "text": TRANSCRIPT}]}],
    }

    flush_memory._keep_session_record(record, flush_memory._capture_now)

    assert written and written[0][0]["session"] == "session-10"
    assert "why systemd and not cron?" in written[0][1]


def test_a_session_record_ranks_below_a_compiled_page() -> None:
    """The words are the user's; the page written from them still wins.

    A session record is evidence, not a stated fact, so its authority weight sits
    below both a user-stated page and an ai-derived compiled one.
    """
    from provenance import authority_weight

    assert authority_weight("session") < authority_weight("ai-derived")
    assert authority_weight("session") < authority_weight("user")


def test_the_record_declares_the_session_authority() -> None:
    document = session_evidence.render_session_document({"session": "s"}, TRANSCRIPT)

    assert "source_authority: session" in document


def test_the_corpus_leaves_session_records_alone(tmp_path: Path) -> None:
    """Kept on disk, read by consolidation, and deliberately not indexed.

    They were collected until 2026-08-24, when importing 236 past sessions took
    the vault stand from hit@5 0.7 to 0.0: hundreds of half-megabyte transcripts
    of the same conversations the pages were compiled from crowded the compiled
    pages out of the candidate pool entirely. Neither a below-neutral trust
    weight nor ordering compiled pages first recovered more than 0.4.
    """
    import corpus_snapshot

    vault = tmp_path / "vault"
    (vault / "knowledge/notes").mkdir(parents=True)
    (vault / "knowledge/notes/page.md").write_text(
        "---\ntype: decision\n---\n# Page\nContent.\n", encoding="utf-8"
    )
    record = vault / "knowledge/raw/sessions/2026-08-23/abc123.md"
    record.parent.mkdir(parents=True)
    record.write_text(
        session_evidence.render_session_document(
            {"session": "abc123", "captured_at": "2026-08-23T10:00:00Z"}, TRANSCRIPT
        ),
        encoding="utf-8",
    )

    snapshot = corpus_snapshot.collect_corpus(vault)

    paths = [source.record.relative_path for source in snapshot.sources]
    assert paths == ["knowledge/notes/page.md"]
    assert record.exists()


def test_a_session_chunk_carries_the_session_kind(tmp_path: Path) -> None:
    import corpus_snapshot

    document = session_evidence.render_session_document({"session": "abc123"}, TRANSCRIPT)
    content = document.encode("utf-8")
    relative = "knowledge/raw/sessions/2026-08-23/abc123.md"

    chunks = corpus_snapshot.canonical_retrieval_chunks(
        source_id=f"source:{relative}",
        source_path=relative,
        source_sha256=__import__("hashlib").sha256(content).hexdigest(),
        content=content,
    )

    assert chunks
    # `type` comes from the record's own frontmatter; the authority is what makes
    # it rank below a compiled page.
    assert all(chunk.type == "raw-source" for chunk in chunks)
    assert all(chunk.authority == "session" for chunk in chunks)


def test_the_record_reaches_disk_through_the_real_transaction(
    tmp_path: Path, monkeypatch
) -> None:
    """No stub here, and that is the point.

    Every other test in this file replaces `mutate_knowledge`, which is how a
    whole feature shipped and wrote nothing for a day: `knowledge/raw/sessions`
    was outside the writer's allowed roots, and the write — which never raises,
    by contract — dropped every session record in silence.
    """
    vault = tmp_path / "vault"
    (vault / "knowledge/raw").mkdir(parents=True)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(vault))

    path = session_evidence.write_session_evidence(
        vault,
        {"session": "real-write", "captured_at": "2026-08-24T09:00:00Z"},
        TRANSCRIPT,
    )

    assert path == vault / "knowledge/raw/sessions/2026-08-24/real-write.md"
    assert path.exists()
    assert "why systemd and not cron?" in path.read_text(encoding="utf-8")
