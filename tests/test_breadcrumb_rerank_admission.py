"""A relevant event reaches scoring without evicting the primary scoring pass."""
from __future__ import annotations

import functools
import time
from dataclasses import replace

import pytest
import reranker
import retrieval


def _candidate(number, *, event=False, vector=0.0):
    folder = 'knowledge/raw/sessions/2026-09-30' if event else 'knowledge/notes'
    path = f'{folder}/source-{number}.md'
    return retrieval._candidate_from_rerank_row({
        'candidate_id': path, 'path': path, 'parent_id': path,
        'rrf_score': 0.01, 'vector_score': vector,
        'authority_weight': 0.7 if event else 1.0,
        'type_weight': 0.6 if event else 1.0,
    })


def _corpus(answers):
    primary = [_candidate(index) for index in range(30)]
    events = [_candidate(index, event=True, vector=1 - index / 100) for index in range(12)]
    # The weighted fused order differs from the raw dense ordering.
    candidates = (*primary, *reversed(events))
    metadata = {item.candidate_id: {'content': 'answer' if item.candidate_id in answers else 'unrelated'}
                for item in candidates}
    return candidates, metadata


@pytest.fixture
def scored(monkeypatch):
    calls = []

    def score(pairs):
        calls.append(pairs)
        return [6.0 if text == 'answer' else -6.0 for _query, text in pairs]

    monkeypatch.setattr(reranker, 'reranker_installed', lambda: True)
    monkeypatch.setattr(reranker, 'rerank', functools.partial(reranker.rerank, scorer=score))
    monkeypatch.setattr(retrieval, '_OPTIONAL_STAGE_OBSERVED', {})
    monkeypatch.setattr(retrieval, '_OPTIONAL_STAGE_OBSERVED_AT', {})
    return calls


def _apply(candidates, metadata, **overrides):
    trace = retrieval._RerankTrace()
    options = dict(analysis=retrieval.analyze_query('find the relevant evidence'),
                   requested='HYBRID', limit=5, max_candidates=None, rerank_enabled=True,
                   deadline_monotonic=None, cancelled=None, trace=trace)
    ranked = retrieval._apply_reranking(candidates, metadata, **{**options, **overrides})
    return ranked, trace


def test_a_cross_language_event_below_the_primary_prefix_reaches_scoring(scored):
    answer = _candidate(6, event=True).candidate_id
    candidates, metadata = _corpus({answer})
    ranked, trace = _apply(candidates, metadata)
    assert answer in [item.candidate_id for item in retrieval._page_diverse(ranked)[:5]]
    assert trace.applied
    assert sum(len(call) for call in scored) == 2 * reranker.DEFAULT_RERANK_DEPTH
    assert {item.candidate_id for item in ranked} == {item.candidate_id for item in candidates}


def test_relevant_events_have_no_fixed_output_quota(scored):
    answers = {_candidate(index, event=True).candidate_id for index in range(6)}
    candidates, metadata = _corpus(answers)
    ranked, _trace = _apply(candidates, metadata)
    assert {item.candidate_id for item in retrieval._page_diverse(ranked)[:5]} <= answers


def test_a_single_primary_does_not_prevent_event_scoring(scored):
    answer = _candidate(6, event=True).candidate_id
    candidates, metadata = _corpus({answer})
    candidates = (candidates[0], *candidates[30:])
    ranked, _trace = _apply(candidates, metadata)
    assert retrieval._page_diverse(ranked)[0].candidate_id == answer


def test_secondary_failure_preserves_the_completed_primary_result(monkeypatch, scored):
    original = reranker.rerank

    def fail_events(query, rows, **options):
        if rows[0]['path'].startswith('knowledge/raw/sessions/'):
            raise OSError('event scorer failed')
        return original(query, rows, **options)

    monkeypatch.setattr(reranker, 'rerank', fail_events)
    answer = _candidate(4).candidate_id
    candidates, metadata = _corpus({answer})
    ranked, trace = _apply(candidates, metadata)
    assert ranked[0].candidate_id == answer
    assert trace.applied and trace.fallback_reason == 'reranker_error'
    assert len(scored) == 1


def test_unavailable_primary_does_not_claim_event_scoring(monkeypatch):
    calls = []

    def unavailable(_query, rows, **_options):
        calls.append(rows)
        return [{**row, 'reranker_applied': False} for row in rows]

    monkeypatch.setattr(reranker, 'reranker_installed', lambda: True)
    monkeypatch.setattr(reranker, 'rerank', unavailable)
    candidates, metadata = _corpus(set())
    ranked, trace = _apply(candidates, metadata)
    assert ranked == candidates and not trace.applied
    assert len(calls) == 1


def test_notes_only_keep_the_existing_single_pass(scored):
    candidates, metadata = _corpus({_candidate(4).candidate_id})
    ranked, trace = _apply(candidates[:30], metadata)
    assert ranked[0].candidate_id == candidates[4].candidate_id
    assert trace.applied and len(scored) == 1


@pytest.mark.parametrize('with_events', [False, True])
def test_semantic_primary_match_reaches_scoring_before_trust_hides_it(scored, with_events):
    answer = _candidate(13).candidate_id
    candidates, metadata = _corpus({answer})
    candidates = tuple(replace(item, vector_score=0.99) if item.candidate_id == answer else item
                       for item in candidates)
    candidates = candidates if with_events else candidates[:30]
    ranked, trace = _apply(candidates, metadata)
    assert ranked[0].candidate_id == answer and trace.applied


def test_an_exact_event_title_keeps_the_existing_bypass(scored):
    candidates, metadata = _corpus(set())
    metadata[candidates[-1].candidate_id]['title'] = 'find the relevant evidence'
    ranked, trace = _apply(candidates, metadata)
    assert ranked[0] == candidates[-1]
    assert not scored and trace.fallback_reason == 'exact_title_bypass'


def test_event_work_shares_the_existing_reranker_capacity():
    assert retrieval._optional_stage_slots('rerank_events') is retrieval._optional_stage_slots('rerank')


def test_long_event_cost_does_not_refuse_a_short_primary_pass(monkeypatch):
    now = time.monotonic()
    monkeypatch.setattr(retrieval, '_OPTIONAL_STAGE_OBSERVED', {'rerank': 0.2, 'rerank_events': 8.0})
    monkeypatch.setattr(retrieval, '_OPTIONAL_STAGE_OBSERVED_AT', {'rerank': now, 'rerank_events': now})
    window = now + 2.0
    assert retrieval._rerank_worker_deadline(window) == window
    with pytest.raises(retrieval.OptionalStageNotAdmitted):
        retrieval._rerank_worker_deadline(window, kind='rerank_events')


def test_secondary_not_admitted_keeps_the_primary_answer_and_reports_the_reason(monkeypatch, scored):
    now = time.monotonic()
    monkeypatch.setattr(retrieval, '_OPTIONAL_STAGE_OBSERVED', {'rerank': 0.01, 'rerank_events': 8.0})
    monkeypatch.setattr(retrieval, '_OPTIONAL_STAGE_OBSERVED_AT', {'rerank': now, 'rerank_events': now})
    answer = _candidate(4).candidate_id
    candidates, metadata = _corpus({answer})
    deadline = now + retrieval.OPTIONAL_STAGE_TAIL_RESERVE_SECONDS + 1.0
    ranked, trace = _apply(candidates, metadata, deadline_monotonic=deadline)
    assert ranked[0].candidate_id == answer and trace.applied
    assert trace.fallback_reason == 'optional_stage_not_admitted'
    assert not trace.optional_timeout and len(scored) == 1


def test_event_work_cannot_launch_beside_an_existing_reranker(monkeypatch):
    import threading

    semaphore = threading.BoundedSemaphore(1)
    monkeypatch.setitem(retrieval._OPTIONAL_STAGE_KIND_SLOTS, 'rerank', semaphore)
    monkeypatch.setattr(reranker, 'rerank', lambda *_a, **_k: pytest.fail('launched without capacity'))
    with semaphore, pytest.raises(retrieval.OptionalStageTimeout, match='capacity exhausted'):
        retrieval._run_reranker([], query='q', pool_limit=1, cancelled=None,
                                deadline_monotonic=time.monotonic() + 30,
                                source_stage='rerank_events')


def test_each_source_pass_keeps_the_original_deadline_and_tail_reserve():
    deadline = time.monotonic() + 5
    for kind in ('rerank', 'rerank_events'):
        stage = retrieval._source_rerank_deadline(deadline, kind)
        assert stage == deadline - retrieval.OPTIONAL_STAGE_TAIL_RESERVE_SECONDS


def test_disabled_reranking_does_not_start_either_pass(scored):
    candidates, metadata = _corpus(set())
    ranked, trace = _apply(candidates, metadata, rerank_enabled=False)
    assert ranked == candidates and not scored and not trace.applied
