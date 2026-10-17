"""Snapshot exclusion ends before a provider call; errors remain errors."""
import json
import shutil
import time

import corpus_snapshot
import fact_keys
import markdown_transaction as mt
import memory_state
import pytest

from tests.test_reliability_v3_adoption import build_adopted_reliability_v3


def _vault(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    (root / 'scripts').mkdir(parents=True)
    (root / 'knowledge/daily').mkdir(parents=True)
    shutil.copyfile(memory_state.ROOT / 'scripts/integration_adapter.py',
                    root / 'scripts/integration_adapter.py')
    (root / 'knowledge/daily/2026-10-05.md').write_text(
        '# 2026-10-05\n\n## [10:00:00] session_end | s\n\n'
        '**user:** I own a blue bicycle.\n\n**assistant:** Noted.\n')
    build_adopted_reliability_v3(root, root)
    monkeypatch.setattr(memory_state, 'ROOT', root)
    monkeypatch.setattr(memory_state, 'STATE_ROOT', root)
    return root


def test_main_excludes_another_writer_only_during_snapshot(tmp_path, monkeypatch):
    root = _vault(tmp_path, monkeypatch)
    coordinator = mt.active_markdown_coordinator(root, root)
    other = mt.active_markdown_coordinator(root, root)
    monkeypatch.setattr(mt, 'active_or_legacy_coordinator', lambda *a, **kw: coordinator)
    original = corpus_snapshot.collect_corpus
    events = []

    def collect(*args, **kwargs):
        assert coordinator.writer_gate_held()
        with pytest.raises(TimeoutError):
            with other.writer_gate(wait_seconds=0):
                pytest.fail('concurrent writer entered during snapshot')
        events.append('snapshot')
        return original(*args, **kwargs)

    def ask(prompt, system_prompt):
        assert not coordinator.writer_gate_held()
        with other.writer_gate(wait_seconds=0):
            events.append('provider_after_release')
        return json.dumps({'0': ['I own a blue bicycle']})

    monkeypatch.setattr(corpus_snapshot, 'collect_corpus', collect)
    monkeypatch.setattr(fact_keys, '_provider_ask', ask)
    monkeypatch.setattr(fact_keys, '_extend_recurring_pages', lambda store: 0)
    assert fact_keys.main([]) == 0
    assert events == ['snapshot', 'provider_after_release']
    store = fact_keys.KeyStore(fact_keys.store_path(root))
    assert store.count() == (1, 1)
    store.close()


def test_failed_collection_releases_the_actual_gate_and_never_calls_provider(tmp_path, monkeypatch):
    root = _vault(tmp_path, monkeypatch)
    coordinator = mt.active_markdown_coordinator(root, root)
    monkeypatch.setattr(mt, 'active_or_legacy_coordinator', lambda *a, **kw: coordinator)
    called = []

    def changed(*args, **kwargs):
        assert coordinator.writer_gate_held()
        raise corpus_snapshot.CorpusChanged('external editor changed source')

    monkeypatch.setattr(corpus_snapshot, 'collect_corpus', changed)
    monkeypatch.setattr(fact_keys, '_provider_ask', lambda *args: called.append(True))
    with pytest.raises(corpus_snapshot.CorpusChanged, match='external editor'):
        fact_keys.main([])
    assert not coordinator.writer_gate_held()
    assert called == []
    with coordinator.writer_gate(wait_seconds=0):
        assert coordinator.writer_gate_held()


def test_busy_gate_is_a_failure_before_collection(tmp_path, monkeypatch):
    root = _vault(tmp_path, monkeypatch)
    coordinator = mt.active_markdown_coordinator(root, root)
    other = mt.active_markdown_coordinator(root, root)
    monkeypatch.setattr(mt, 'active_or_legacy_coordinator', lambda *a, **kw: other)
    called = []
    monkeypatch.setattr(corpus_snapshot, 'collect_corpus', lambda *a, **kw: called.append('collect'))
    monkeypatch.setattr(fact_keys, '_provider_ask', lambda *a: called.append('provider'))
    with coordinator.writer_gate(wait_seconds=0):
        with pytest.raises(TimeoutError):
            fact_keys.main(['--budget-seconds', str(time.get_clock_info('monotonic').resolution)])
    assert called == []
