"""Phase 0.5 regression tests: 3-tier FLUSH classification in flush_memory.

Locks in:
1. `_classify_response` correctly identifies FLUSH_MAJOR / FLUSH_MINOR /
   FLUSH_OK from the new prompt protocol (first line = tier token).
2. Legacy FLUSH_OK responses (from pre-Phase-0.5 summaries still in
   flight or already-persisted daily logs) still classify as tier=ok.
3. `maybe_trigger_compile` only fires for tier="major" — minor/ok
   never spawns a compile process even if hour cutoff is met.
4. Defensive defaults: missing/empty/garbled responses default to the
   safer (lower-tier, no-compile) outcome.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Helpers
#
# These stand-ins are named functions rather than lambdas and comprehensions on
# purpose: the complexity gate counts every one of those against the test that
# holds them, and refuses the whole file.
# ---------------------------------------------------------------------------


def _raises(error):
    """A callable that always fails with `error`."""

    def _call(*args, **kwargs):
        raise error

    return _call


def _kinds(recorded):
    """The first argument of every recorded call."""
    return [call[0] for call in recorded]


# ---------------------------------------------------------------------------
# _classify_response
# ---------------------------------------------------------------------------


def test_classify_major_with_body():
    import flush_memory  # noqa: WPS433

    raw = """FLUSH_MAJOR

**Decisions made**
- Use SHA-256 for compile incrementalism (mtime is unreliable on Windows).

**Lessons / patterns**
- When a daily log is stub-only, skip silently — do not invent content.
"""
    tier, body = flush_memory._classify_response(raw)
    assert tier == "major"
    assert "SHA-256" in body
    assert "Decisions made" in body


def test_classify_minor_with_body():
    import flush_memory  # noqa: WPS433

    raw = """FLUSH_MINOR

**Gotchas / debugging**
- Edit tool "Found N matches" → expand old_string with unique context, do not switch to replace_all.
"""
    tier, body = flush_memory._classify_response(raw)
    assert tier == "minor"
    assert "Gotchas" in body


def test_classify_ok_pure_token():
    import flush_memory  # noqa: WPS433

    tier, body = flush_memory._classify_response("FLUSH_OK")
    assert tier == "ok"
    assert body == ""


def test_classify_ok_with_trailing_whitespace_and_punctuation():
    """LLMs sometimes add stray periods or backticks. Be tolerant."""
    import flush_memory  # noqa: WPS433

    for variant in ["FLUSH_OK.", "`FLUSH_OK`", "FLUSH_OK\n", "  FLUSH_OK  "]:
        tier, body = flush_memory._classify_response(variant)
        assert tier == "ok", f"failed for variant {variant!r}"
        assert body == ""


def test_classify_legacy_flush_ok_anywhere():
    """Legacy protocol had FLUSH_OK as a standalone line anywhere in
    the response. Must still be recognized.
    """
    import flush_memory  # noqa: WPS433

    legacy = """Some preamble about the session.

FLUSH_OK
"""
    tier, body = flush_memory._classify_response(legacy)
    assert tier == "ok"
    assert body == ""


def test_classify_empty_and_none():
    import flush_memory  # noqa: WPS433

    assert flush_memory._classify_response("") == ("ok", "")
    assert flush_memory._classify_response(None) == ("ok", "")  # type: ignore[arg-type]
    assert flush_memory._classify_response("   \n\t  ") == ("ok", "")


def test_classify_summary_failed_sentinel():
    """Crash sentinel from the SDK path classifies as ok (skip)."""
    import flush_memory  # noqa: WPS433

    tier, body = flush_memory._classify_response("(summary failed: RuntimeError)")
    assert tier == "ok"
    assert body == ""


def test_classify_no_sentinel_defaults_to_minor():
    """Defensive: if the LLM emits content with no recognized tier
    token, we treat it as MINOR. Better to save potentially-useful
    content than to lose it. Does NOT trigger compile (safer).
    """
    import flush_memory  # noqa: WPS433

    raw = """Some random content with no tier token at all.
Just narrative that the LLM produced without following protocol."""
    tier, body = flush_memory._classify_response(raw)
    assert tier == "minor"
    assert "random content" in body


def test_classify_case_insensitive_token():
    """Tier tokens are case-insensitive (LLM may emit lower or mixed)."""
    import flush_memory  # noqa: WPS433

    for variant in ["flush_major", "Flush_Major", "FLUSH_MAJOR"]:
        tier, _ = flush_memory._classify_response(variant + "\n\nbody")
        assert tier == "major", f"failed for {variant!r}"


# ---------------------------------------------------------------------------
# maybe_trigger_compile — tier gating
# ---------------------------------------------------------------------------


