"""A failed compile batch is recorded and the later ones still run (audit 2026-09-27 A-3).

docs/research/2026-09-27-one-bad-day-does-not-hold-the-rest.md
"""
from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace

from reliable_memory import canonical_json_bytes, sha256_bytes

from tests.test_compile_transactions import vault  # noqa: F401 - fixture

BUDGET = {"provider": "fake", "model": "fake-v1", "max_output_tokens": 4000}


def _plan(inputs, batch) -> SimpleNamespace:
    key = sha256_bytes(canonical_json_bytes([item.logical_path for item in inputs.dailies]))
    plan = {"schema_version": "compile-plan/v2", "operations": []}
    return SimpleNamespace(
        plan=plan, action_key=key, cache_hit=False, provider_budget=BUDGET, batch=batch,
    )


def test_a_failed_oldest_day_does_not_stop_the_newer_one(vault, monkeypatch):  # noqa: F811
    import compile_memory

    root, _state_root = vault
    older = root / "knowledge/daily/2026-07-14.md"
    newer = root / "knowledge/daily/2026-07-15.md"
    older.write_bytes(b"a" * 14_000)
    newer.write_bytes(b"b" * 14_000)
    state: dict[str, object] = {"last_compile_status": "running"}
    seen_statuses = []

    def resolve(inputs, cache, *, coordinator, batch, token_adapters=None):
        seen_statuses.append(state["last_compile_status"])
        if any(item.logical_path.endswith(older.name) for item in inputs.dailies):
            raise ValueError("evidence block is ambiguous or missing")
        return _plan(inputs, batch)

    monkeypatch.setattr(compile_memory, "load_state", lambda: state)
    monkeypatch.setattr(compile_memory, "update_state", lambda mutate: mutate(state))
    monkeypatch.setattr(compile_memory, "resolve_compile_plan", resolve)

    result = compile_memory._run(Namespace(file=None, all=False, dry_run=False, trigger="manual"))

    assert (result, state["compiled_daily_hashes"]) == (1, {newer.name: sha256_bytes(newer.read_bytes())})
    assert seen_statuses == ["running", "running"]
    assert state["last_compile_status"] == "error"


def test_failed_batch_keeps_run_status_and_compile_lock(vault, monkeypatch):  # noqa: F811
    import os

    import compile_memory as c
    import maybe_compile

    root, state_root = vault
    day = root / 'knowledge/daily/2026-07-14.md'
    day.write_text('## 12:00:00\nA source line.\n')
    state = {'last_compile_status': 'running'}
    monkeypatch.setattr(c, 'update_state', lambda mutate: mutate(state))
    monkeypatch.setattr(maybe_compile, 'LOCK_FILE', state_root / 'run/compile.pid')
    maybe_compile._write_lock(os.getpid())
    before = maybe_compile.LOCK_FILE.read_bytes()
    inputs = c.snapshot_compile_inputs([day])

    code = c._failed_compile(Namespace(trigger='manual', dry_run=False), inputs, ValueError('bad quote'))

    assert code == 1
    assert state == {'last_compile_status': 'running'}
    assert maybe_compile.LOCK_FILE.read_bytes() == before


def test_failed_run_finalizes_only_after_all_batch_outcomes(vault, monkeypatch):  # noqa: F811
    import compile_memory as c

    finished = []
    monkeypatch.setattr(c, '_mark_finished', lambda *args, **kwargs: finished.append((args, kwargs)))
    outcomes = [c.BatchOutcome(1), c.BatchOutcome(0, outcome='published', paths=1)]

    result = c._finish_run(Namespace(trigger='manual', dry_run=False), outcomes)

    assert result == 1
    assert len(finished) == 1
    assert finished[0][0][:2] == ('manual', 'error')
    assert '1 batch' in finished[0][0][2]


def test_failed_dry_run_records_no_source_failure_or_outcome(vault, monkeypatch):  # noqa: F811
    import compile_memory as c

    recorded = []
    monkeypatch.setattr(c, '_record_compile_source_failures', lambda *a, **k: recorded.append('source'))
    monkeypatch.setattr(c, '_mark_finished', lambda *a, **k: recorded.append('finished'))
    args = Namespace(trigger='manual', dry_run=True)
    outcome = c._failed_batch(args, c.CompileInputs((), (), ()), ValueError('bad quote'))

    assert c._finish_run(args, [outcome]) == 1
    assert recorded == []
