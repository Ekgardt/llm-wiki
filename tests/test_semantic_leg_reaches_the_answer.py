"""Regression tests for the four ways the semantic leg failed to reach an answer.

Measured on the live vault on 2026-08-26 and fixed in the same pass:

  1. `OPTIONAL_STAGE_MAX_SECONDS` was 0.5 s while the warm dense leg costs about
     one second, so no caller that passed a deadline could ever use it.
  2. `retrieve_via_search_memory` never resolved a query encoder, so the entry
     point the grounded answer uses got `generation_vectors_unavailable`.
  3. A bare `после` / `after` routed a topic question to `TEMPORAL`, which
     declares no dense signal at all.
  4. The grounded answer passed its answer size as `max_candidates`, a resource
     cap on each backend's pool, collapsing the pool to the answer size.

And the fifth, which threw the first real answer away: the provider fenced it.
"""
from __future__ import annotations

import time

import pytest


def test_a_bare_preposition_is_not_a_time_window() -> None:
    """`после` in a topic name is sequence, not a date filter."""
    from retrieval import analyze_query

    analysis = analyze_query("как устроен повтор после карантина")

    assert analysis.recommended_profile == "HYBRID"
    assert "temporal" not in analysis.intents


@pytest.mark.parametrize(
    "question",
    [
        "decisions since 2025-01-01",
        "решения с 2025-01-01",
        "自 2025-01-01 以来的决策",
        "what changed after 2026-01-01",
    ],
)
def test_an_anchored_preposition_is_still_a_time_window(question: str) -> None:
    """Every pinned temporal case names a real time expression, and still routes."""
    from retrieval import analyze_query

    assert analyze_query(question).recommended_profile == "TEMPORAL"


def test_an_optional_stage_may_outlast_a_warm_dense_leg() -> None:
    """The ceiling must clear the stage it exists to permit.

    Measured: the warm dense leg of one recall costs 0.99-1.33 s. At the old
    0.5 s ceiling this call raised instead of returning.
    """
    import retrieval

    deadline = time.monotonic() + 60.0
    value = retrieval._run_optional_bounded(
        lambda: (time.sleep(0.8), "dense rows")[1],
        deadline=deadline,
        cancelled=None,
    )

    assert value == "dense rows"
    assert retrieval.OPTIONAL_STAGE_MAX_SECONDS > 1.33


def test_the_lower_entry_point_resolves_its_own_query_encoder() -> None:
    """`search()` resolves an encoder before delegating; this entry must too."""
    import retrieval
    import search_memory

    encoder, model_id, revision = retrieval._resolved_query_encoder(
        search_memory, semantic=True, embedder=None, model_id=None, model_revision=None
    )

    assert callable(encoder)
    assert model_id == search_memory.EMBEDDING_MODEL
    assert revision == search_memory.EMBEDDING_MODEL_REVISION


def test_a_supplied_encoder_is_left_exactly_as_the_caller_passed_it() -> None:
    import retrieval
    import search_memory

    supplied = object()

    resolved = retrieval._resolved_query_encoder(
        search_memory,
        semantic=True,
        embedder=supplied,
        model_id="m",
        model_revision="r",
    )

    assert resolved == (supplied, "m", "r")


def test_no_encoder_is_resolved_when_the_caller_did_not_ask_for_meaning() -> None:
    import retrieval
    import search_memory

    resolved = retrieval._resolved_query_encoder(
        search_memory, semantic=False, embedder=None, model_id=None, model_revision=None
    )

    assert resolved == (None, None, None)


def test_an_unavailable_model_yields_no_vector_rather_than_raising(monkeypatch) -> None:
    """The generation reader already treats an unusable query vector as no dense."""
    import search_memory

    monkeypatch.setattr(search_memory, "_get_embedder", lambda: None)
    encode = search_memory._lazy_generation_query_encoder()

    assert encode(["question"]) == []


def test_the_grounded_answer_does_not_cap_its_own_candidate_pool(monkeypatch) -> None:
    """`max_candidates` is a resource cap on each backend, not the answer size."""
    import query_memory
    import retrieval

    seen: dict[str, object] = {}

    def fake_retrieve(question, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(retrieval, "retrieve_via_search_memory", fake_retrieve)
    query_memory._default_candidates(
        "вопрос", profile="HYBRID", deadline=time.monotonic() + 60
    )

    assert seen.get("limit") == query_memory.QA_MAX_CANDIDATES
    assert "max_candidates" not in seen


def test_a_fenced_answer_is_still_an_answer() -> None:
    """A correct answer was thrown away for three backticks."""
    import query_memory

    parsed = query_memory._parsed_answer('```json\n{"status": "answered"}\n```')

    assert parsed == {"status": "answered"}


def test_prose_wrapped_around_a_fence_no_longer_throws_the_answer_away() -> None:
    """Refusing these cost fifteen complete answers in 200 questions.

    Measured 2026-09-02. Every one of the fifteen carried a valid document
    inside a fence and a sentence of commentary outside it. The fence is now
    taken wherever it sits; what protects the reader is the schema validation
    and the citation gates that run afterwards, not the shape of the wrapper.
    """
    import query_memory

    parsed = query_memory._parsed_answer(
        'here you go:\n```json\n{"a": 1}\n```\nhope that helps'
    )

    assert parsed == {"a": 1}


def test_a_reply_that_is_only_prose_is_still_refused() -> None:
    import query_memory

    with pytest.raises(query_memory.GroundedQAError):
        query_memory._parsed_answer("here you go: the answer is 600 followers")


def test_an_agent_worktree_belongs_to_the_checkout_that_owns_it() -> None:
    """A subagent's worktree is a copy of one project, not a project."""
    from pathlib import Path

    from session_start_project_state import owning_checkout

    worktree = Path("/home/user/llm-wiki/.claude/worktrees/agent-a0a60df15")

    assert owning_checkout(worktree) == Path("/home/user/llm-wiki")


def test_a_worktree_the_owner_made_elsewhere_stays_its_own_project() -> None:
    """Nothing here can tell whether that separation was deliberate."""
    from pathlib import Path

    from session_start_project_state import owning_checkout

    elsewhere = Path("/home/user/worktrees/feature-branch")

    assert owning_checkout(elsewhere) == elsewhere
