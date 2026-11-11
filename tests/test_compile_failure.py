"""Regression test: compile_memory fail-safe semantics (Round 1 #C2).

Verifies that a failed LLM compile run does NOT:
  - mark the daily log as compiled (compiled_daily_hashes unchanged)
  - produce a zero exit code
  - append a fake success entry to knowledge/log.md
And DOES:
  - record last_compile_status = "error" in state.json

Before the R1 fix, a rate-limited or crashed compile silently marked the
daily as compiled, causing the next run to skip it — class "silent data
loss". Keeping this test in the suite guards against re-regression.
"""
from __future__ import annotations

import json

import pytest

from tests.compile_failure_helpers import _attempt
from tests.compile_paths import point_compile_path


@pytest.fixture
def isolated_compile_state(tmp_path, monkeypatch):
    """Give failed compiles disposable state and restore cached paths afterward."""

    state_root = tmp_path / "state"
    state_dir = state_root / "run"
    state_file = state_dir / "state.json"
    state_dir.mkdir(parents=True)
    state_file.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    return {
        "state_root": state_root,
        "state_file": state_file,
        "state_before": "{}\n",
        "log_md": tmp_path / "vault" / "knowledge" / "log.local.md",
    }


def test_failed_compile_does_not_mark_hash(isolated_compile_state, monkeypatch):
    import compile_pipeline
    from llm_wiki import compile_pipeline_selection

    state_snapshot = isolated_compile_state
    vault = state_snapshot["log_md"].parents[1]
    daily_dir = vault / "knowledge/daily"
    notes = vault / "knowledge/notes"
    daily_dir.mkdir(parents=True)
    notes.mkdir(parents=True)
    fake_daily = daily_dir / "2026-01-01.md"
    fake_daily.write_text("snapshot", encoding="utf-8")
    agents = vault / "AGENTS.md"
    agents.write_text("contract", encoding="utf-8")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    point_compile_path(monkeypatch, "resolve_compile_plan", lambda inputs, cache, **kwargs: (_ for _ in ()).throw(
            RuntimeError("regression-test-induced")
        ))

    # On CI there are no daily logs (gitignored for privacy), so
    # select_dailies returns [] and main() exits 0 before calling
    # run_compile. Monkeypatch select_dailies to return a fake path
    # INSIDE the vault root (compile_memory does relative_to(ROOT)
    # on each daily path for display).
    point_compile_path(monkeypatch, "select_dailies", lambda args, state, **kwargs: [fake_daily])
    monkeypatch.setattr("sys.argv", ["compile_memory.py", "--trigger", "manual"])

    state_before = json.loads(state_snapshot["state_before"])
    hashes_before = dict(state_before.get("compiled_daily_hashes", {}))

    try:
        # This fixture exercises the retained legacy batch executor explicitly.
        # Adopted foreground failure is covered by test_compile_foreground_queue.
        exit_code = compile_pipeline_selection.compile_under_lock(
            compile_pipeline_selection.parse_args(), execute=compile_pipeline.run)
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1

    # Exit code must be non-zero
    assert exit_code == 1, f"expected exit=1 on failure, got {exit_code}"

    state_after = json.loads(state_snapshot["state_file"].read_text(encoding="utf-8"))

    # Hashes unchanged — the critical invariant
    hashes_after = dict(state_after.get("compiled_daily_hashes", {}))
    assert hashes_after == hashes_before, (
        f"compiled_daily_hashes MUTATED on failed compile: "
        f"added {set(hashes_after) - set(hashes_before)}"
    )

    # Status must record the error
    assert state_after.get("last_compile_status") == "error", (
        f"expected last_compile_status=error, got "
        f"{state_after.get('last_compile_status')!r}"
    )

    # the vault log must not have gained a fake success entry
    log_after = state_snapshot["log_md"].read_text(encoding="utf-8") if state_snapshot["log_md"].exists() else ""
    assert log_after == "", (
        "the vault log changed on failed compile (expected no append)"
    )

    from queue_core import (
        MemoryQueue,
    )
    from reliable_memory import sha256_bytes

    failure = MemoryQueue(state_snapshot["state_root"]).source_failure(
        "knowledge/daily/2026-01-01.md",
        sha256_bytes(b"snapshot"),
    )
    assert failure is not None
    assert failure["producer"] == "compile"
    assert failure["error_code"] == "RuntimeError"

    monkeypatch.undo()


def test_a_stochastic_validation_failure_is_retried(monkeypatch):
    """One malformed generation used to lose the whole nightly compile.

    Evidence: the nightly of 2026-08-22 reported
    `critique:claude:<implicit>:validation_error` while the same inputs resolved
    a plan minutes earlier and minutes later.
    """
    from dataclasses import replace

    import compile_pipeline

    attempt, calls = _attempt(monkeypatch, ["validation_error", "validation_error"])
    candidate = compile_pipeline.provider_candidates("", max_tokens=4000)[0]

    assert attempt.resolve(replace(candidate)) == "plan"
    assert len(calls) == 3
    assert attempt.lineage.count("critique:" + candidate.identity + ":validation_error") == 2


def test_a_provider_failure_is_not_retried(monkeypatch):
    """Only the stochastic class is retried; a provider that is down stays down."""
    from dataclasses import replace

    import compile_pipeline

    attempt, calls = _attempt(monkeypatch, ["provider_error", "provider_error"])
    candidate = compile_pipeline.provider_candidates("", max_tokens=4000)[0]

    assert attempt.resolve(replace(candidate)) is None
    assert len(calls) == 1


def test_the_validation_retry_is_bounded(monkeypatch):
    """Retries cost tokens, so they stop and the lineage shows every one."""
    from dataclasses import replace

    import compile_pipeline
    import settings

    attempt, calls = _attempt(monkeypatch, ["validation_error"] * 10)
    candidate = compile_pipeline.provider_candidates("", max_tokens=4000)[0]

    assert attempt.resolve(replace(candidate)) is None
    assert len(calls) == settings.setting_value('compile.validation_retries') + 1


def test_a_lock_that_cannot_be_read_refuses_the_run_and_says_why(
    isolated_compile_state, monkeypatch
):
    """Doubt refuses: a lock failure is not "the spawner owns it" (audit H4)."""
    from llm_wiki import compile_pipeline_selection

    def unreadable() -> bool:
        raise OSError("run/compile.pid: permission denied")

    point_compile_path(monkeypatch, "try_claim_lock", unreadable)

    outcome, reason = compile_pipeline_selection.acquire_compile_lock()

    assert (outcome, reason) == (
        None,
        "compile lock unavailable (OSError: run/compile.pid: permission denied)",
    )


def test_a_held_lock_names_its_holder(isolated_compile_state, monkeypatch):
    from llm_wiki import compile_pipeline_selection

    point_compile_path(monkeypatch, "try_claim_lock", lambda: False)
    point_compile_path(monkeypatch, "lock_state", lambda: ("live", "running pid=7 since t"))

    outcome, reason = compile_pipeline_selection.acquire_compile_lock()

    assert (outcome, reason) == (None, "lock held by another compile (running pid=7 since t)")
