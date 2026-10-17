"""A failed compile batch is recorded and the later ones still run (audit 2026-09-27 A-3).

docs/research/2026-09-27-one-bad-day-does-not-hold-the-rest.md
"""
from __future__ import annotations

import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from reliable_memory import canonical_json_bytes, sha256_bytes

from tests.test_compile_transactions import vault  # noqa: F401 - fixture

BUDGET = {"provider": "fake", "model": "fake-v1", "max_output_tokens": 4000}


def _plan(inputs) -> SimpleNamespace:
    key = sha256_bytes(canonical_json_bytes([item.logical_path for item in inputs.dailies]))
    plan = {"schema_version": "compile-plan/v2", "operations": []}
    return SimpleNamespace(plan=plan, action_key=key, cache_hit=False, provider_budget=BUDGET)


def test_a_failed_oldest_day_does_not_stop_the_newer_one(vault, monkeypatch):  # noqa: F811
    import compile_memory

    root, _state_root = vault
    older = root / "knowledge/daily/2026-07-14.md"
    newer = root / "knowledge/daily/2026-07-15.md"
    older.write_bytes(b"a" * 14_000)
    newer.write_bytes(b"b" * 14_000)
    state: dict[str, object] = {}

    def resolve(inputs, cache, *, coordinator, batch, token_adapters=None):
        if any(item.logical_path.endswith(older.name) for item in inputs.dailies):
            raise ValueError("evidence block is ambiguous or missing")
        return _plan(inputs)

    monkeypatch.setattr(compile_memory, "load_state", lambda: state)
    monkeypatch.setattr(compile_memory, "update_state", lambda mutate: mutate(state))
    monkeypatch.setattr(compile_memory, "_mark_finished", lambda *args, **kwargs: None)
    monkeypatch.setattr(compile_memory, "resolve_compile_plan", resolve)

    result = compile_memory._run(Namespace(file=None, all=False, dry_run=False, trigger="manual"))

    assert (result, state["compiled_daily_hashes"]) == (1, {newer.name: sha256_bytes(newer.read_bytes())})


def _competing_claim(root, state_root) -> bool:
    import maybe_compile

    program = "import sys;sys.path.insert(0,sys.argv[1]);import maybe_compile;print(maybe_compile._claim_lock())"
    done = subprocess.run(
        [sys.executable, "-c", program, str(Path(maybe_compile.__file__).parent)],
        env={**os.environ, "LLM_WIKI_ROOT": str(root), "LLM_WIKI_STATE_ROOT": str(state_root)},
        capture_output=True, text=True, check=True, timeout=30,
    )
    return done.stdout.strip() == "True"


def test_a_failed_batch_keeps_ownership_until_later_batches_finish(vault, monkeypatch):  # noqa: F811
    import compile_memory
    import maybe_compile

    root, state_root = vault
    older = root / "knowledge/daily/2026-07-14.md"
    newer = root / "knowledge/daily/2026-07-15.md"
    older.write_bytes(b"a" * 14_000)
    newer.write_bytes(b"b" * 14_000)
    state = {}
    observations = []
    monkeypatch.setattr(maybe_compile, "LOCK_FILE", state_root / "run/compile.pid")
    monkeypatch.setattr(compile_memory, "load_state", lambda: state)
    monkeypatch.setattr(compile_memory, "update_state", lambda mutate: mutate(state))

    def resolve(inputs, cache, *, coordinator, batch, token_adapters=None):
        if any(item.logical_path.endswith(older.name) for item in inputs.dailies):
            raise ValueError("first batch deliberately fails")
        observations.append((maybe_compile._lock_state()[0], state["last_compile_status"]))
        observations.append(("last_compile_finished_at" in state, _competing_claim(root, state_root)))
        return _plan(inputs)

    monkeypatch.setattr(compile_memory, "resolve_compile_plan", resolve)
    result = compile_memory._compile_under_lock(
        Namespace(file=None, all=False, dry_run=False, trigger="manual")
    )

    assert observations == [("live", "running"), (False, False)]
    assert (result, state["last_compile_status"], state["last_compile_outcome"]) == (1, "error", "failed")
    assert state["compiled_daily_hashes"] == {newer.name: sha256_bytes(newer.read_bytes())}
    assert "first batch deliberately fails" in state["last_compile_error"]
    assert not maybe_compile.LOCK_FILE.exists()
