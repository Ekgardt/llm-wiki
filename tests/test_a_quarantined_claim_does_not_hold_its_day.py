"""A quarantined claim is held on its page; it does not hold back the batch.

A search hit quarantined every new claim, and one quarantined claim kept the
whole day unpublished until a person reviewed it, which nobody does. See
docs/research/2026-09-25-a-quarantined-claim-does-not-hold-its-day.md.
"""

from __future__ import annotations

from llm_wiki import compile_pipeline_claims, compile_pipeline_sources


def _published(*touched: str) -> compile_pipeline_sources.CompileApplyResult:
    return compile_pipeline_sources.CompileApplyResult(
        "t", "compile:" + "a" * 64, "committed", touched, 1, "2026-09-25T00:00:00Z", "b" * 64
    )


def test_a_batch_with_a_held_claim_says_it_published_and_what_it_held(capsys) -> None:
    outcome = compile_pipeline_claims.committed_outcome(
        _published("knowledge/notes/page.md", "knowledge/index.md", "knowledge/inbox/claims/x.md")
    )

    assert outcome.outcome == "published"
    assert "published 2 page(s); 1 claim(s) quarantined under knowledge/inbox/claims/" in capsys.readouterr().out


def test_a_batch_with_no_held_claim_says_only_what_it_published(capsys) -> None:
    compile_pipeline_claims.committed_outcome(_published("knowledge/notes/page.md"))

    assert capsys.readouterr().out.strip() == "compile_memory: batch published 1 page(s)."
