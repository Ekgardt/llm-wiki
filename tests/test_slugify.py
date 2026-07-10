"""Regression test: query_memory.slugify is Unicode-safe and collision-resistant.

Covers Round 3 #I5 (Cyrillic preservation) and Round 5 #5
(punct-only / emoji-only → deterministic hash fallback).
"""
from __future__ import annotations

from query_memory import slugify


def test_cyrillic_preserved():
    s = slugify("Как работает hook?")
    assert s.startswith("как-работает-hook")
    # Must have a hash suffix
    assert len(s) > len("как-работает-hook")


def test_cyrillic_two_questions_distinct():
    s1 = slugify("Что такое slug?")
    s2 = slugify("Где лежит state.md?")
    assert s1 != s2


def test_slug_is_deterministic():
    assert slugify("test question") == slugify("test question")


def test_punct_only_inputs_distinct():
    """Round 5 #5: `???`, `!!!`, emoji — all must get distinct slugs."""
    inputs = ["???", "!!!", "💥", "...", "~~~"]
    slugs = [slugify(q) for q in inputs]
    assert len(set(slugs)) == len(slugs), (
        f"punct-only inputs collapsed to fewer slugs than inputs: {dict(zip(inputs, slugs))}"
    )
    for s in slugs:
        assert s.startswith("question-"), f"expected 'question-<hash>', got {s!r}"


def test_empty_input_deterministic():
    s1 = slugify("")
    s2 = slugify("")
    assert s1 == s2
    assert s1.startswith("question")


def test_no_ascii_destruction():
    """Previous `[^a-z0-9]+` would collapse all Cyrillic to `-`.

    The new `[^\\w]+` with re.UNICODE preserves letters in any script.
    Verify at least one Cyrillic char survives.
    """
    s = slugify("Какой slug?")
    # "какой" — check first letter preserved as 'к'
    assert "к" in s


def test_length_respects_max():
    """Long inputs are truncated to max_len (default 60)."""
    long_q = "what " * 40  # way over 60 chars
    s = slugify(long_q)
    assert len(s) <= 60, f"slug exceeds default max_len: {len(s)} chars"


def test_max_len_smaller_than_hash_suffix():
    """max_len=5 (< 7 = '-' + 6-hex hash) still produces a deterministic slug.

    The hash suffix always survives; the slug head is clipped to zero.
    The result is longer than max_len (unavoidable — the hash alone is
    6 chars), but the function must not crash and must remain
    deterministic.
    """
    s1 = slugify("hello world", max_len=5)
    s2 = slugify("hello world", max_len=5)
    assert s1 == s2, "slug must be deterministic even with tiny max_len"
    # The 6-char hash is always present.
    assert len(s1) >= 6, f"hash suffix missing: {s1!r}"
