"""The six items of C-F15 the first round left: reporting, naming, dating and re-entry.

See `docs/research/2026-09-17-the-six-capture-corrections-the-first-round-left.md`.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backfill_sessions  # noqa: E402
import flush_memory  # noqa: E402
import integration_adapter  # noqa: E402
import session_start_context  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _completed(stdout: str, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, returncode=returncode)


def test_a_skipped_tag_is_not_reported_as_a_written_daily_log():
    reports = (
        _completed(json.dumps({"daily_log_written": False})),
        _completed(json.dumps({"daily_log_written": True})),
        _completed("", returncode=1),
        _completed(""),
    )

    written = tuple(integration_adapter._delegate_wrote_daily_log(r) for r in reports)

    assert written == (False, True, False, True)


def test_the_tag_delegate_says_it_skipped_when_it_has_no_vault(tmp_path, monkeypatch):
    """Exit 0 and a plain answer: the hook must not fail, but it must not claim a write."""
    environment = {key: value for key, value in __import__("os").environ.items()}
    environment.pop("LLM_WIKI_ROOT", None)
    environment["CLAUDE_PROJECT_DIR"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "session_end_project_tag.py")],
        input="{}",
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )

    assert (result.returncode, json.loads(result.stdout)) == (0, {"daily_log_written": False})


def test_the_per_line_legacy_sentinel_is_recognised():
    replies = ("FLUSH_OK", "(no durable content)", "some\n(no durable content)\nlines")

    assert [flush_memory._classify_response(reply)[0] for reply in replies] == ["ok"] * 3


def test_a_session_record_authority_is_named_by_the_contract_and_is_not_a_claim_authority():
    from claims import _EXTRACTION_ENUMS  # noqa: PLC0415
    from provenance import authority_weight  # noqa: PLC0415

    claim_authorities = dict(_EXTRACTION_ENUMS)["authority"]
    contract = (Path(__file__).resolve().parent.parent / "CLAUDE.md").read_text("utf-8")
    ranked = authority_weight("inferred") < authority_weight("session") < authority_weight("ai-derived")

    assert ("source_authority: session" in contract, "session" in claim_authorities, ranked) == (
        True,
        False,
        True,
    )


def test_a_backfilled_record_is_named_and_dated_as_live_capture_would(tmp_path):
    """Same session, same name, same day — so the second path leaves the first alone."""
    transcript = tmp_path / "rollout-2026-09-16T12-00-00-abc.jsonl"
    transcript.write_bytes(
        json.dumps({"payload": {"id": "0199c0de-1234", "cwd": str(tmp_path)}}).encode("utf-8")
        + b"\n"
    )
    day = datetime.fromtimestamp(transcript.stat().st_mtime).date().isoformat()

    fields = backfill_sessions._fields(transcript, backfill_sessions._session_day(transcript))

    assert (fields["session"], fields["captured_at"][:10]) == ("0199c0de-1234", day)


def test_the_scan_says_how_many_transcripts_its_cap_left_out():
    outcome = backfill_sessions.Outcome(scanned=3, unscanned=7)

    lines = outcome.as_lines(applied=False)

    assert lines[-1].startswith(
        f"left unscanned by the {backfill_sessions.MAX_TRANSCRIPTS}-transcript cap: 7"
    )


def test_the_advisory_is_built_for_the_session_that_is_starting(monkeypatch):
    asked: list[str | None] = []
    monkeypatch.setitem(
        sys.modules,
        "build_advisory",
        SimpleNamespace(build_advisory=lambda slug: asked.append(slug) or ""),
    )
    monkeypatch.setattr(
        session_start_context, "_latest_heartbeat_slug", lambda: "another-project"
    )

    session_start_context.advisory_block("this-project")
    session_start_context.advisory_block()

    assert asked == ["this-project", "another-project"]


def test_a_host_event_raised_inside_the_memory_system_is_not_captured(monkeypatch):
    monkeypatch.setenv(integration_adapter.REENTRY_MARKER, "memory-automation")
    ingested: list[object] = []
    monkeypatch.setattr(integration_adapter, "ingest_event", ingested.append)

    exit_code = integration_adapter.main(["--source", "claude", "--event", "session_end"])

    assert (exit_code, ingested) == (0, [])


@pytest.mark.parametrize("marker", ["", "   "])
def test_a_host_event_outside_the_memory_system_is_captured(monkeypatch, marker):
    monkeypatch.setenv(integration_adapter.REENTRY_MARKER, marker)

    assert integration_adapter._is_memory_automation() is False
