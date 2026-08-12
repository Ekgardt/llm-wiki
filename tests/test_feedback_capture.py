"""Tests for feedback_capture.py — correction/preference detection."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def test_detect_correction():
    """'No, use X instead' → correction."""
    import feedback_capture
    ftype, conf = feedback_capture._detect_feedback_type(
        "No, we should use JWT instead of sessions because stateless"
    )
    assert ftype == "correction"
    assert conf >= 0.5


def test_detect_preference():
    """'I prefer concise answers' → preference."""
    import feedback_capture
    ftype, conf = feedback_capture._detect_feedback_type(
        "I prefer concise answers without unnecessary preamble"
    )
    assert ftype == "preference"
    assert conf >= 0.5


def test_detect_instruction():
    """'Remember that we use Postgres' → instruction."""
    import feedback_capture
    ftype, conf = feedback_capture._detect_feedback_type(
        "Remember that we always use Postgres for the database layer"
    )
    assert ftype in ("instruction", "preference")
    assert conf >= 0.5


def test_detect_rejection():
    """'That's not right' → rejection or correction (both are valid feedback)."""
    import feedback_capture
    ftype, conf = feedback_capture._detect_feedback_type(
        "That's not right, the auth middleware should validate first"
    )
    assert ftype in ("rejection", "correction")  # "not" matches both patterns
    assert conf >= 0.5


def test_detect_noise_filtered():
    """'ok thanks' → None (noise)."""
    import feedback_capture
    ftype, conf = feedback_capture._detect_feedback_type("ok thanks")
    assert ftype is None
    assert conf == 0.0


def test_detect_short_text_filtered():
    """Text < 10 chars → None."""
    import feedback_capture
    ftype, conf = feedback_capture._detect_feedback_type("hi")
    assert ftype is None


def test_detect_empty_text():
    """Empty string → None."""
    import feedback_capture
    assert feedback_capture._detect_feedback_type("")[0] is None
    assert feedback_capture._detect_feedback_type(None)[0] is None


def test_detect_multiple_patterns_higher_confidence():
    """Multiple pattern matches → higher confidence."""
    import feedback_capture
    # Contains both "no" and "should" and "must"
    ftype, conf = feedback_capture._detect_feedback_type(
        "No, this must be changed, we should always validate inputs"
    )
    assert conf >= 0.7  # multiple patterns boost confidence


def test_ordinary_sentences_with_broad_modal_words_are_not_feedback():
    import feedback_capture

    ordinary_sentences = [
        "The report is not available until the nightly job finishes.",
        "Workers must acquire the lock before writing shared state.",
        "The service will stop after the final queue item is processed.",
        "We need to inspect the existing implementation before changing it.",
    ]

    for text in ordinary_sentences:
        assert feedback_capture._detect_feedback_type(text) == (None, 0.0)


def test_explicit_corrections_and_preferences_remain_feedback():
    import feedback_capture

    for text in [
        "No, use PostgreSQL instead of SQLite for production.",
        "That's not right; validate the signature before reading claims.",
        "I prefer concise answers without an introductory paragraph.",
        "Never use force push in this repository.",
    ]:
        assert feedback_capture._detect_feedback_type(text)[0] is not None


def test_bare_wrong_and_incorrect_in_factual_status_are_not_feedback():
    import feedback_capture

    factual_status = [
        "The wrong value was written to cache during migration.",
        "The report contains an incorrect checksum for the artifact.",
    ]

    for text in factual_status:
        assert feedback_capture._detect_feedback_type(text) == (None, 0.0)


def test_conversational_wrong_and_incorrect_phrases_are_feedback():
    import feedback_capture

    corrections = [
        "That's wrong, use the project-specific configuration instead.",
        "You are wrong about the retry limit; it is three attempts.",
        "That is incorrect; the worker deletes the file before classification.",
        "Your timeout calculation is incorrect because it excludes startup time.",
    ]

    for text in corrections:
        assert feedback_capture._detect_feedback_type(text)[0] in {
            "correction",
            "rejection",
        }


def test_capture_from_text_saves_candidate(tmp_path, monkeypatch):
    """Valid correction saves a JSON candidate file."""
    import feedback_capture

    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", tmp_path)
    cid = feedback_capture.capture_from_text(
        "No, we should use JWT instead of sessions because stateless matters",
        session_id="test-session",
        slug="test-project",
        trigger="test",
    )
    assert cid is not None
    candidate_file = tmp_path / f"{cid}.json"
    assert candidate_file.exists()
    candidate = json.loads(candidate_file.read_text())
    assert candidate["type"] == "correction"
    assert candidate["status"] == "candidate"
    assert candidate["session_id"] == "test-session"


def _owned_feedback_project(
    tmp_path: Path,
    monkeypatch,
) -> tuple[object, Path, Path]:
    import feedback_capture

    vault = tmp_path / "vault"
    projects = vault / "knowledge" / "projects"
    project = tmp_path / "work" / "feedback-project"
    state_path = projects / "feedback-project" / "state.md"
    project.mkdir(parents=True)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        "# Feedback project state\n"
        f"- Project root JSON: {json.dumps(str(project.resolve()))}\n"
        '- Runtime slug JSON: "feedback-project"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(feedback_capture, "ROOT", vault)
    monkeypatch.setattr(feedback_capture, "PROJECTS_DIR", projects)
    monkeypatch.setattr(
        feedback_capture,
        "FEEDBACK_DIR",
        vault / "knowledge" / "feedback",
    )
    return feedback_capture, project, state_path


def test_capture_persists_only_confirmed_canonical_project_identity(
    tmp_path,
    monkeypatch,
):
    feedback_capture, project, _state_path = _owned_feedback_project(
        tmp_path,
        monkeypatch,
    )

    cid = feedback_capture.capture_from_text(
        "No, always preserve the confirmed project root for feedback.",
        session_id="confirmed-session",
        slug="feedback-project",
        project_root=str(project / "."),
        trigger="test",
    )

    assert cid is not None
    candidate = json.loads(
        (feedback_capture.FEEDBACK_DIR / f"{cid}.json").read_text(encoding="utf-8")
    )
    assert candidate["project"] == "feedback-project"
    assert candidate["project_root"] == str(project.resolve())


@pytest.mark.parametrize("slug", ("other-project", "feedback-project-copy"))
def test_capture_rejects_unconfirmed_feedback_identity(
    tmp_path,
    monkeypatch,
    slug: str,
):
    feedback_capture, project, _state_path = _owned_feedback_project(
        tmp_path,
        monkeypatch,
    )

    cid = feedback_capture.capture_from_text(
        "No, never persist feedback under an unconfirmed identity.",
        session_id="rejected-session",
        slug=slug,
        project_root=project,
        trigger="test",
    )

    assert cid is None
    assert not feedback_capture.FEEDBACK_DIR.exists()


def test_capture_from_text_rejects_noise(tmp_path, monkeypatch):
    """Noise text returns None, no file saved."""
    import feedback_capture

    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", tmp_path)
    cid = feedback_capture.capture_from_text("ok cool thanks")
    assert cid is None
    assert len(list(tmp_path.glob("*.json"))) == 0


def test_feedback_cli_rejects_mixed_role_transcript_scanning(tmp_path, monkeypatch):
    import feedback_capture

    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"role":"assistant","text":"No, use the AI summary instead"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["feedback_capture.py", "capture", "--transcript", str(transcript)],
    )

    with pytest.raises(SystemExit) as exc_info:
        feedback_capture.main()

    assert exc_info.value.code == 2


def test_feedback_stdin_rejects_oversized_input_before_capture(monkeypatch):
    import feedback_capture

    class BoundedOnlyInput(io.StringIO):
        def __init__(self, value: str):
            super().__init__(value)
            self.request_sizes: list[int] = []

        def read(self, size: int = -1) -> str:
            self.request_sizes.append(size)
            assert size > 0, "reader requested an unbounded allocation"
            return super().read(size)

    payload = json.dumps(
        {
            "text": "No, use the bounded reader instead of reading to EOF.",
            "session_id": "session-oversized",
            "slug": "project",
            "padding": "x" * 256,
        }
    )
    stream = BoundedOnlyInput(payload)
    captured: list[tuple] = []
    monkeypatch.setattr(feedback_capture, "MAX_HOOK_STDIN_BYTES", 64, raising=False)
    monkeypatch.setattr(feedback_capture, "capture_from_text", lambda *args, **kwargs: captured.append((args, kwargs)))
    monkeypatch.setattr(sys, "argv", ["feedback_capture.py"])
    monkeypatch.setattr(sys, "stdin", stream)

    assert feedback_capture.main() == 0
    assert stream.request_sizes and all(size > 0 for size in stream.request_sizes)
    assert captured == []


def test_feedback_stdin_rejects_unpaired_surrogate_without_candidate(
    tmp_path,
    monkeypatch,
):
    import feedback_capture

    feedback_dir = tmp_path / "feedback"
    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", feedback_dir)
    monkeypatch.setattr(sys, "argv", ["feedback_capture.py"])
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            r'{"text":"No, use \ud800 instead of this invalid value.",'
            r'"session_id":"session-1","slug":"project"}'
        ),
    )

    assert feedback_capture.main() == 0
    assert not feedback_dir.exists()


def test_list_candidates(tmp_path, monkeypatch):
    """list_candidates returns only candidates with matching status."""
    import feedback_capture

    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", tmp_path)
    feedback_capture.capture_from_text(
        "No, use JWT instead", session_id="s1", slug="p1", trigger="t"
    )
    candidates = feedback_capture.list_candidates("candidate")
    assert len(candidates) == 1


@pytest.mark.parametrize(
    "hostile_kind",
    (
        "oversized",
        "deeply-nested",
        "invalid-utf8",
        "non-object",
        "non-finite",
        "huge-integer",
        "surrogate",
        "invalid-fields",
    ),
)
def test_list_candidates_skips_hostile_json_sibling(
    tmp_path,
    monkeypatch,
    hostile_kind,
):
    import feedback_capture

    valid = {
        "id": "a" * 12,
        "type": "correction",
        "confidence": 0.7,
        "text": "No, keep the valid candidate.",
        "session_id": "session-1",
        "project": "project-1",
        "trigger": "test",
        "captured_at": "2026-07-30T12:00:00",
        "status": "candidate",
    }
    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", tmp_path)
    monkeypatch.setattr(feedback_capture, "MAX_FEEDBACK_BYTES", 1024, raising=False)
    monkeypatch.setattr(feedback_capture, "MAX_FEEDBACK_JSON_DEPTH", 8, raising=False)
    (tmp_path / "00-valid.json").write_text(json.dumps(valid), encoding="utf-8")

    hostile_path = tmp_path / "99-hostile.json"
    hostile = {**valid, "id": "b" * 12}
    if hostile_kind == "oversized":
        hostile["padding"] = "x" * 1024
        hostile_path.write_text(json.dumps(hostile), encoding="utf-8")
    elif hostile_kind == "deeply-nested":
        nested = None
        for _ in range(12):
            nested = [nested]
        hostile["nested"] = nested
        hostile_path.write_text(json.dumps(hostile), encoding="utf-8")
    elif hostile_kind == "invalid-utf8":
        hostile_path.write_bytes(b'\xff{"status":"candidate"}')
    elif hostile_kind == "non-object":
        hostile_path.write_text("[]", encoding="utf-8")
    elif hostile_kind == "non-finite":
        hostile["confidence"] = float("nan")
        hostile_path.write_text(json.dumps(hostile), encoding="utf-8")
    elif hostile_kind == "huge-integer":
        hostile["confidence"] = 10**400
        hostile_path.write_text(json.dumps(hostile), encoding="utf-8")
    elif hostile_kind == "surrogate":
        hostile["text"] = "\ud800"
        hostile_path.write_text(json.dumps(hostile), encoding="utf-8")
    else:
        hostile["id"] = []
        hostile_path.write_text(json.dumps(hostile), encoding="utf-8")

    assert feedback_capture.list_candidates("candidate") == [valid]


def test_list_candidates_fails_closed_when_feedback_inventory_overflows(
    tmp_path,
    monkeypatch,
):
    import feedback_capture

    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", tmp_path)
    monkeypatch.setattr(
        feedback_capture,
        "MAX_FEEDBACK_FILES_SCANNED",
        1,
        raising=False,
    )
    for index in range(2):
        (tmp_path / f"{index}.json").write_text(
            json.dumps({"status": "candidate"}),
            encoding="utf-8",
        )

    assert feedback_capture.list_candidates("candidate") == []


def test_list_candidates_isolates_feedback_file_memory_error(tmp_path, monkeypatch):
    import feedback_capture

    valid = {
        "id": "a" * 12,
        "type": "correction",
        "confidence": 0.7,
        "text": "No, preserve the valid candidate.",
        "session_id": "session-1",
        "project": "project-1",
        "trigger": "test",
        "captured_at": "2026-07-30T12:00:00",
        "status": "candidate",
    }
    hostile = tmp_path / "00-hostile.json"
    hostile.write_text(json.dumps(valid), encoding="utf-8")
    (tmp_path / "01-valid.json").write_text(json.dumps(valid), encoding="utf-8")
    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", tmp_path)
    real_open = Path.open

    def fail_hostile_read(path, *args, **kwargs):
        if path == hostile:
            raise MemoryError("injected feedback read exhaustion")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_hostile_read)

    assert feedback_capture.list_candidates("candidate") == [valid]


def test_promote_candidate_creates_knowledge_page(tmp_path, monkeypatch):
    """Promoting a candidate creates a knowledge .md file."""
    import feedback_capture

    # Create a candidate first
    monkeypatch.setattr(feedback_capture, "FEEDBACK_DIR", tmp_path / "feedback")
    feedback_capture.FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    cid = feedback_capture.capture_from_text(
        "No, use JWT instead of sessions because stateless",
        session_id="s1", slug="p1", trigger="test",
    )

    # Promote it
    knowledge_dir = tmp_path / "knowledge" / "patterns"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(feedback_capture, "ROOT", tmp_path)

    result = feedback_capture.promote_candidate(cid, "patterns")
    assert result is not None
    assert result.endswith(".md")

    # Candidate status updated
    candidate_file = tmp_path / "feedback" / f"{cid}.json"
    candidate = json.loads(candidate_file.read_text())
    assert candidate["status"] == "promoted"


def test_promoted_feedback_frontmatter_preserves_confirmed_project_root(
    tmp_path,
    monkeypatch,
):
    feedback_capture, project, _state_path = _owned_feedback_project(
        tmp_path,
        monkeypatch,
    )
    cid = feedback_capture.capture_from_text(
        "No, always carry the confirmed root into promoted feedback.",
        session_id="promotion-session",
        slug="feedback-project",
        project_root=project,
        trigger="test",
    )
    assert cid is not None

    promoted = feedback_capture.promote_candidate(cid, "patterns")

    assert promoted is not None
    page = (feedback_capture.ROOT / promoted).read_text(encoding="utf-8")
    assert "project: feedback-project\n" in page
    assert f"project_root: {json.dumps(str(project.resolve()))}\n" in page
