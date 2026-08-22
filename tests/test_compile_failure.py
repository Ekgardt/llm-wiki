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


@pytest.fixture
def isolated_compile_state(tmp_path, monkeypatch):
    """Give failed compiles disposable state and restore cached paths afterward."""
    import compile_memory
    import maybe_compile
    import memory_state

    state_root = tmp_path / "state"
    state_dir = state_root / "run"
    state_file = state_dir / "state.json"
    state_dir.mkdir(parents=True)
    state_file.write_text("{}\n", encoding="utf-8")
    owner_before = maybe_compile._current_owner
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setattr(memory_state, "STATE_ROOT", state_root)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_file)
    monkeypatch.setattr(memory_state, "LOCK_FILE", state_dir / "state.json.lock")
    monkeypatch.setattr(compile_memory, "STATE_ROOT", state_root)
    monkeypatch.setattr(maybe_compile, "STATE_ROOT", state_root)
    monkeypatch.setattr(maybe_compile, "_current_owner", None)
    monkeypatch.setattr(maybe_compile, "LOCK_FILE", state_dir / "compile.pid")
    monkeypatch.setattr(
        maybe_compile, "LOG_OUT", state_root / "logs" / "maybe-compile-last.log"
    )
    monkeypatch.setattr(
        maybe_compile, "LOG_ERR", state_root / "logs" / "maybe-compile-last.err.log"
    )
    return {
        "owner_before": owner_before,
        "state_root": state_root,
        "state_file": state_file,
        "state_before": "{}\n",
        "log_md": tmp_path / "test-log.md",
    }


def test_failed_compile_does_not_mark_hash(isolated_compile_state, monkeypatch):
    import compile_memory  # noqa: WPS433
    import maybe_compile

    state_snapshot = isolated_compile_state
    vault = state_snapshot["log_md"].parent / "vault"
    daily_dir = vault / "knowledge/daily"
    notes = vault / "knowledge/notes"
    daily_dir.mkdir(parents=True)
    notes.mkdir(parents=True)
    fake_daily = daily_dir / "2026-01-01.md"
    fake_daily.write_text("snapshot", encoding="utf-8")
    agents = vault / "AGENTS.md"
    agents.write_text("contract", encoding="utf-8")
    monkeypatch.setattr(compile_memory, "ROOT", vault)
    monkeypatch.setattr(compile_memory, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", notes)
    monkeypatch.setattr(compile_memory, "INDEX", vault / "knowledge/index.md")
    monkeypatch.setattr(compile_memory, "LOG", state_snapshot["log_md"])
    monkeypatch.setattr(compile_memory, "AGENTS", agents)
    monkeypatch.setattr(
        compile_memory,
        "resolve_compile_plan",
        lambda inputs, cache, **kwargs: (_ for _ in ()).throw(
            RuntimeError("regression-test-induced")
        ),
    )

    # On CI there are no daily logs (gitignored for privacy), so
    # select_dailies returns [] and main() exits 0 before calling
    # run_compile. Monkeypatch select_dailies to return a fake path
    # INSIDE the vault root (compile_memory does relative_to(ROOT)
    # on each daily path for display).
    monkeypatch.setattr(
        compile_memory,
        "select_dailies",
        lambda args, state, **kwargs: [fake_daily],
    )
    monkeypatch.setattr("sys.argv", ["compile_memory.py", "--trigger", "manual"])

    state_before = json.loads(state_snapshot["state_before"])
    hashes_before = dict(state_before.get("compiled_daily_hashes", {}))

    try:
        exit_code = compile_memory.main()
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

    # knowledge/log.md (redirected to tmp) must not have gained a fake success entry
    log_after = state_snapshot["log_md"].read_text(encoding="utf-8") if state_snapshot["log_md"].exists() else ""
    assert log_after == "", (
        "knowledge/log.md changed on failed compile (expected no append)"
    )

    from memory_queue import MemoryQueue
    from reliable_memory import sha256_bytes

    failure = MemoryQueue(state_snapshot["state_root"]).source_failure(
        "knowledge/daily/2026-01-01.md",
        sha256_bytes(b"snapshot"),
    )
    assert failure is not None
    assert failure["producer"] == "compile"
    assert failure["error_code"] == "RuntimeError"

    monkeypatch.undo()
    assert maybe_compile._current_owner == state_snapshot["owner_before"]


def _attempt(monkeypatch, outcomes: list[str]):
    """One provider attempt whose draft stage yields the given failure codes."""
    import compile_memory

    calls: list[int] = []

    def staged(self, descriptor, actions):
        index = len(calls)
        calls.append(1)
        if index >= len(outcomes):
            return "plan"
        return self._record("critique", descriptor, outcomes[index])

    monkeypatch.setattr(compile_memory, "probe_candidate", lambda _d: True)
    monkeypatch.setattr(
        compile_memory._CompileAttempt, "_actions", lambda self, _d: ((), ())
    )
    monkeypatch.setattr(
        compile_memory._CompileAttempt, "_cached", lambda self, _a, _d: None
    )
    monkeypatch.setattr(compile_memory._CompileAttempt, "_drafted", staged)
    attempt = compile_memory._CompileAttempt(
        compile_memory.CompileInputs((), (), ()), None, None, None
    )
    return attempt, calls


def test_a_stochastic_validation_failure_is_retried(monkeypatch):
    """One malformed generation used to lose the whole nightly compile.

    Evidence: the nightly of 2026-08-22 reported
    `critique:claude:<implicit>:validation_error` while the same inputs resolved
    a plan minutes earlier and minutes later.
    """
    from dataclasses import replace

    import compile_memory

    attempt, calls = _attempt(monkeypatch, ["validation_error", "validation_error"])
    candidate = compile_memory.provider_candidates("", max_tokens=4000)[0]

    assert attempt.resolve(replace(candidate)) == "plan"
    assert len(calls) == 3
    assert attempt.lineage.count("critique:" + candidate.identity + ":validation_error") == 2


def test_a_provider_failure_is_not_retried(monkeypatch):
    """Only the stochastic class is retried; a provider that is down stays down."""
    from dataclasses import replace

    import compile_memory

    attempt, calls = _attempt(monkeypatch, ["provider_error", "provider_error"])
    candidate = compile_memory.provider_candidates("", max_tokens=4000)[0]

    assert attempt.resolve(replace(candidate)) is None
    assert len(calls) == 1


def test_the_validation_retry_is_bounded(monkeypatch):
    """Retries cost tokens, so they stop and the lineage shows every one."""
    from dataclasses import replace

    import compile_memory

    attempt, calls = _attempt(monkeypatch, ["validation_error"] * 10)
    candidate = compile_memory.provider_candidates("", max_tokens=4000)[0]

    assert attempt.resolve(replace(candidate)) is None
    assert len(calls) == compile_memory.VALIDATION_RETRIES + 1
