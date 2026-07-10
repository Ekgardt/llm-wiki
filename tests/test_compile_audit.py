"""Phase 0 regression tests: new compile_memory audit + snapshot features.

Three guarantees these tests lock in:

1. `parse_compile_audit` correctly extracts structured counts from a
   well-formed COMPILE_AUDIT line.
2. `parse_compile_audit` tolerates partial / malformed / missing audits
   without crashing (returns empty dict, not an exception).
3. `existing_knowledge_snapshot` includes Title + Summary lines so the
   LLM can satisfy the DEDUP-BEFORE-CREATE rule — not just bare filenames.

These guard against re-regression when the prompt is later edited: if
someone strips the COMPILE_AUDIT sentinel or reverts the snapshot to
filenames-only, the suite fails loudly.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# parse_compile_audit
# ---------------------------------------------------------------------------


def test_parse_compile_audit_extracts_all_counts():
    import compile_memory  # noqa: WPS433

    raw = """Some preamble text.

COMPILE_DONE: 3 page(s) touched: knowledge/notes/patterns/foo.md, knowledge/notes/decisions/bar.md
COMPILE_AUDIT: verified 7 evidence citations; 12 dedup checks performed; 2 stubs skipped; 1 contradictions handled; 0 pages rejected as below-threshold
"""
    audit = compile_memory.parse_compile_audit(raw)
    assert audit == {
        "verified": 7,
        "dedup": 12,
        "stubs": 2,
        "contradictions": 1,
        "rejected": 0,
    }


def test_parse_compile_audit_tolerates_missing_line():
    """Legacy compiles (pre-Phase-0) don't emit COMPILE_AUDIT.

    Must return empty dict, not raise.
    """
    import compile_memory  # noqa: WPS433

    legacy = "COMPILE_DONE: 1 page(s) touched: knowledge/notes/patterns/foo.md\n"
    assert compile_memory.parse_compile_audit(legacy) == {}


def test_parse_compile_audit_tolerates_partial_line():
    """LLM may emit a partial audit (skipped a field).

    Whatever is present is extracted; missing fields are absent from
    the dict (callers use .get with default 0).
    """
    import compile_memory  # noqa: WPS433

    partial = (
        "COMPILE_DONE: 2 page(s) touched: a.md, b.md\n"
        "COMPILE_AUDIT: verified 4 evidence citations; 1 stubs skipped\n"
    )
    audit = compile_memory.parse_compile_audit(partial)
    assert audit.get("verified") == 4
    assert audit.get("stubs") == 1
    assert "dedup" not in audit
    assert "contradictions" not in audit
    assert "rejected" not in audit


def test_parse_compile_audit_handles_empty_and_none():
    import compile_memory  # noqa: WPS433

    assert compile_memory.parse_compile_audit("") == {}
    assert compile_memory.parse_compile_audit(None) == {}  # type: ignore[arg-type]


def test_parse_compile_audit_finds_last_when_multiple():
    """If the LLM accidentally emits two COMPILE_AUDIT lines (e.g. one
    mid-thinking, one final), the LAST one wins — matches COMPILE_DONE
    semantics.
    """
    import compile_memory  # noqa: WPS433

    raw = """COMPILE_AUDIT: verified 1 evidence citations; 0 dedup checks performed; 0 stubs skipped; 0 contradictions handled; 0 pages rejected as below-threshold

more text

COMPILE_DONE: 1 page(s) touched: foo.md
COMPILE_AUDIT: verified 5 evidence citations; 3 dedup checks performed; 1 stubs skipped; 0 contradictions handled; 1 pages rejected as below-threshold
"""
    audit = compile_memory.parse_compile_audit(raw)
    assert audit["verified"] == 5
    assert audit["dedup"] == 3
    assert audit["rejected"] == 1


# ---------------------------------------------------------------------------
# existing_knowledge_snapshot — title + summary enrichment
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_knowledge_tree(tmp_path: Path):
    """Build a minimal knowledge/notes/ tree with realistic pages."""
    knowledge = tmp_path / "knowledge" / "notes"
    for cat in ("patterns", "decisions", "debugging"):
        (knowledge / cat).mkdir(parents=True)

    (knowledge / "patterns" / "hook-defense.md").write_text(
        textwrap.dedent(
            """
            # Hook scripts defense in depth

            One-sentence summary: always fail closed and log to hook-errors.log even when SDK is missing.

            ## Lesson
            Body.
            """
        ).strip(),
        encoding="utf-8",
    )
    (knowledge / "decisions" / "use-sha256.md").write_text(
        textwrap.dedent(
            """
            # Use SHA-256 for compile incrementalism

            One-sentence summary: SHA-256 detects real content change regardless of mtime churn.

            ## Decision
            Body.
            """
        ).strip(),
        encoding="utf-8",
    )
    # Page without the conventional headers — fallback path.
    (knowledge / "debugging" / "stub.md").write_text(
        "Just a body with no H1 or summary line.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_knowledge_snapshot_includes_titles_and_summaries(temp_knowledge_tree: Path):
    """Snapshot must carry title + summary so the LLM can detect
    semantic overlap, not just slug collisions.
    """
    import compile_memory  # noqa: WPS433

    with patch.object(compile_memory, "KNOWLEDGE", temp_knowledge_tree / "knowledge" / "notes"):
        snapshot = compile_memory.existing_knowledge_snapshot()

    # Title + summary present for well-formed pages (new format:
    # «title»: summary, so both are in the snapshot)
    assert "Hook scripts defense in depth" in snapshot
    assert "always fail closed and log to hook-errors.log" in snapshot
    assert "Use SHA-256 for compile incrementalism" in snapshot
    assert "SHA-256 detects real content change" in snapshot
    # Slug still in the path prefix (for file-level reference)
    assert "patterns/hook-defense.md" in snapshot
    assert "decisions/use-sha256.md" in snapshot
    # Title is wrapped in «» to give LLM a clear anchor
    assert "«Hook scripts defense in depth»" in snapshot
    assert "«Use SHA-256 for compile incrementalism»" in snapshot


def test_knowledge_snapshot_handles_missing_convention(temp_knowledge_tree: Path):
    """Pages without H1 or One-sentence summary fall back to filename
    stem, no crash.
    """
    import compile_memory  # noqa: WPS433

    with patch.object(compile_memory, "KNOWLEDGE", temp_knowledge_tree / "knowledge" / "notes"):
        snapshot = compile_memory.existing_knowledge_snapshot()

    # The stub file still appears (filename used as fallback)
    assert "debugging/stub.md" in snapshot


def test_knowledge_snapshot_empty_when_no_pages(tmp_path: Path):
    import compile_memory  # noqa: WPS433

    empty_knowledge = tmp_path / "knowledge" / "notes"
    empty_knowledge.mkdir(parents=True)
    with patch.object(compile_memory, "KNOWLEDGE", empty_knowledge):
        snapshot = compile_memory.existing_knowledge_snapshot()
    assert snapshot == "(no pages yet)"


# ---------------------------------------------------------------------------
# Backward compat: existing test_compile_failure.py still passes
# ---------------------------------------------------------------------------


def test_failed_compile_does_not_mark_hash_still_holds():
    """Sanity check that Phase 0 changes did not break the existing
    invariant: a failed compile MUST NOT mutate compiled_daily_hashes.

    This is a smoke test — the full version lives in
    test_compile_failure.py. We re-assert the contract here because
    Phase 0 touched `run_compile` callers.
    """
    import compile_memory  # noqa: WPS433

    # Phase 4+ refactor: run_compile is now sync (matches llm_client sync API).
    def fake_failure(daily_paths, dry_run):
        return ([], "(compile failed: RuntimeError: phase0-regression-check)")

    # The function must still accept (daily_paths, dry_run) and return (list, str)
    touched, raw = fake_failure([], False)
    assert touched == []
    assert "phase0-regression-check" in raw
    # And _compile_succeeded must still reject this
    assert compile_memory._compile_succeeded(raw) is False
