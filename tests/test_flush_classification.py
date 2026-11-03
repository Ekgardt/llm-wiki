"""Original classification protocol regression assertions, isolated from capture IO."""
from __future__ import annotations


def test_classify_major_with_body():
    from llm_wiki import classification_protocol as flush_memory

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
    from llm_wiki import classification_protocol as flush_memory

    raw = """FLUSH_MINOR

**Gotchas / debugging**
- Edit tool "Found N matches" → expand old_string with unique context, do not switch to replace_all.
"""
    tier, body = flush_memory._classify_response(raw)
    assert tier == "minor"
    assert "Gotchas" in body


def test_classify_ok_pure_token():
    from llm_wiki import classification_protocol as flush_memory

    tier, body = flush_memory._classify_response("FLUSH_OK")
    assert tier == "ok"
    assert body == ""


def test_classify_ok_with_trailing_whitespace_and_punctuation():
    """LLMs sometimes add stray periods or backticks. Be tolerant."""
    from llm_wiki import classification_protocol as flush_memory

    for variant in ["FLUSH_OK.", "`FLUSH_OK`", "FLUSH_OK\n", "  FLUSH_OK  "]:
        tier, body = flush_memory._classify_response(variant)
        assert tier == "ok", f"failed for variant {variant!r}"
        assert body == ""


def test_classify_legacy_flush_ok_anywhere():
    """Legacy protocol had FLUSH_OK as a standalone line anywhere in
    the response. Must still be recognized.
    """
    from llm_wiki import classification_protocol as flush_memory

    legacy = """Some preamble about the session.

FLUSH_OK
"""
    tier, body = flush_memory._classify_response(legacy)
    assert tier == "ok"
    assert body == ""


def test_classify_empty_and_none():
    from llm_wiki import classification_protocol as flush_memory

    assert flush_memory._classify_response("") == ("ok", "")
    assert flush_memory._classify_response(None) == ("ok", "")  # type: ignore[arg-type]
    assert flush_memory._classify_response("   \n\t  ") == ("ok", "")


def test_classify_summary_failed_sentinel():
    """Crash sentinel from the SDK path classifies as ok (skip)."""
    from llm_wiki import classification_protocol as flush_memory

    tier, body = flush_memory._classify_response("(summary failed: RuntimeError)")
    assert tier == "ok"
    assert body == ""


def test_classify_no_sentinel_defaults_to_minor():
    """Defensive: if the LLM emits content with no recognized tier
    token, we treat it as MINOR. Better to save potentially-useful
    content than to lose it. Does NOT trigger compile (safer).
    """
    from llm_wiki import classification_protocol as flush_memory

    raw = """Some random content with no tier token at all.
Just narrative that the LLM produced without following protocol."""
    tier, body = flush_memory._classify_response(raw)
    assert tier == "minor"
    assert "random content" in body


def test_classify_case_insensitive_token():
    """Tier tokens are case-insensitive (LLM may emit lower or mixed)."""
    from llm_wiki import classification_protocol as flush_memory

    for variant in ["flush_major", "Flush_Major", "FLUSH_MAJOR"]:
        tier, _ = flush_memory._classify_response(variant + "\n\nbody")
        assert tier == "major", f"failed for {variant!r}"
