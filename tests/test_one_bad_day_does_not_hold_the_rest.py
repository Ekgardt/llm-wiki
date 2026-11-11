"""A failed compile batch is recorded and the later ones still run (audit 2026-09-27 A-3).

docs/research/2026-09-27-one-bad-day-does-not-hold-the-rest.md
"""
from __future__ import annotations

import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

from reliable_memory import sha256_bytes

from tests.compile_paths import point_compile_path
from tests.compile_transactions_helpers import vault  # noqa: F401
from tests.one_bad_day_does_not_hold_the_rest_helpers import _plan


def test_a_failed_oldest_day_does_not_stop_the_newer_one(vault, monkeypatch):  # noqa: F811
    import compile_pipeline

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

    point_compile_path(monkeypatch, "load_state", lambda: state)
    point_compile_path(monkeypatch, "update_state", lambda mutate: mutate(state))
    point_compile_path(monkeypatch, "mark_finished", lambda *args, **kwargs: None)
    point_compile_path(monkeypatch, "resolve_compile_plan", resolve)

    result = compile_pipeline.run(Namespace(file=None, all=False, dry_run=False, trigger="manual"))

    assert (result, state["compiled_daily_hashes"]) == (1, {newer.name: sha256_bytes(newer.read_bytes())})


def _competing_claim(root, state_root) -> bool:
    import maybe_compile

    program = "import sys;sys.path.insert(0,sys.argv[1]);import maybe_compile;print(maybe_compile.claim_lock())"
    done = subprocess.run(
        [sys.executable, "-c", program, str(Path(maybe_compile.__file__).parent)],
        env={**os.environ, "LLM_WIKI_ROOT": str(root), "LLM_WIKI_STATE_ROOT": str(state_root)},
        capture_output=True, text=True, check=True, timeout=30,
    )
    return done.stdout.strip() == "True"


def test_a_failed_batch_keeps_ownership_until_later_batches_finish(vault, monkeypatch):  # noqa: F811
    import compile_lock
    import compile_pipeline
    from llm_wiki import compile_pipeline_selection

    root, state_root = vault
    older = root / "knowledge/daily/2026-07-14.md"
    newer = root / "knowledge/daily/2026-07-15.md"
    older.write_bytes(b"a" * 14_000)
    newer.write_bytes(b"b" * 14_000)
    state = {}
    observations = []
    point_compile_path(monkeypatch, "load_state", lambda: state)
    point_compile_path(monkeypatch, "update_state", lambda mutate: mutate(state))

    def resolve(inputs, cache, *, coordinator, batch, token_adapters=None):
        if any(item.logical_path.endswith(older.name) for item in inputs.dailies):
            raise ValueError("first batch deliberately fails")
        observations.append((compile_lock.lock_state()[0], state["last_compile_status"]))
        observations.append(("last_compile_finished_at" in state, _competing_claim(root, state_root)))
        return _plan(inputs)

    point_compile_path(monkeypatch, "resolve_compile_plan", resolve)
    result = compile_pipeline_selection.compile_under_lock(
        Namespace(file=None, all=False, dry_run=False, trigger="manual"),
        execute=compile_pipeline.run,
    )

    assert observations == [("live", "running"), (False, False)]
    assert (result, state["last_compile_status"], state["last_compile_outcome"]) == (1, "error", "failed")
    assert state["compiled_daily_hashes"] == {newer.name: sha256_bytes(newer.read_bytes())}
    assert "first batch deliberately fails" in state["last_compile_error"]
    assert not compile_lock.compile_lock_file().exists()
