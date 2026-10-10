"""Cooperative cancellation stops subsequent native batches, never half-reranks."""
import inspect
import threading

import pytest
import reranker


def _documents():
    return [{'content': 'x' * length, 'summary': 'original', 'score': 1.0}
            for length in (5, 6, 7, 8, 9, 10)]


def _call(documents, cancelled, **options):
    # Original-control runs must exercise the actual old API and count its batches.
    if 'cancelled' in inspect.signature(reranker.rerank).parameters:
        options['cancelled'] = cancelled
    return reranker.rerank('query', documents, limit=len(documents), text_field='content', **options)


def test_cancellation_after_one_native_batch_discards_all_partial_scores(monkeypatch):
    cancelled = threading.Event()
    batches = []
    monkeypatch.setattr(reranker, '_get_reranker_bundle', lambda: {'model_id': 'm', 'model_revision': 'r'})

    def score(_bundle, chosen):
        batches.append(chosen)
        cancelled.set()
        return [3.0] * len(chosen)

    monkeypatch.setattr(reranker, '_batch_logits', score)
    original = _documents()
    result = _call(original, cancelled.is_set)
    assert len(batches) == 1
    assert [item['content'] for item in result] == [item['content'] for item in original]
    assert all(item['reranker_applied'] is False for item in result)
    assert {item['reranker_fallback_reason'] for item in result} == {'reranker_cancelled'}


@pytest.mark.parametrize('already_cancelled', [True, False])
def test_custom_scorer_is_not_given_new_provider_arguments_or_partial_authority(already_cancelled):
    cancelled = threading.Event()
    calls = []
    if already_cancelled:
        cancelled.set()

    def scorer(pairs):
        calls.append(pairs)
        cancelled.set()
        return [1.0] * len(pairs)

    result = _call(_documents(), cancelled.is_set, scorer=scorer)
    assert len(calls) == int(not already_cancelled)
    assert all(item['reranker_applied'] is False for item in result)
    assert {item['reranker_fallback_reason'] for item in result} == {'reranker_cancelled'}


def test_none_cancellation_preserves_actual_batch_order_and_complete_scores(monkeypatch):
    monkeypatch.setattr(reranker, '_get_reranker_bundle', lambda: {'model_id': 'm', 'model_revision': 'r'})
    monkeypatch.setattr(reranker, '_batch_logits', lambda _bundle, chosen: [float(len(pair[1])) for pair in chosen])
    result = _call(_documents(), None)
    assert all(item['reranker_applied'] for item in result)
    assert len(result) == 6
