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
def state_snapshot(tmp_path):
    """Save / restore state.json around the test.

    Log is redirected to tmp_path via monkeypatch in the test itself
    so the real knowledge/log.md is never touched.
    """
    from memory_state import STATE_FILE

    state_file = STATE_FILE

    if not state_file.exists():
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("{}\n", encoding="utf-8")

    state_before = state_file.read_text(encoding="utf-8")

    yield {
        "state_file": state_file,
        "state_before": state_before,
        "log_md": tmp_path / "test-log.md",
    }

    state_file.write_text(state_before, encoding="utf-8")


def test_failed_compile_does_not_mark_hash(state_snapshot, monkeypatch):
    import compile_memory  # noqa: WPS433

    monkeypatch.setattr(compile_memory, "LOG", state_snapshot["log_md"])

    # Patch run_compile to simulate a failure (no LLM response).
    # Phase 4+ refactor: run_compile is now sync (was async when it
    # used claude_agent-sdk; now uses llm_client.call_llm which is sync).
    def fake_failure(daily_paths, dry_run):
        return ([], "(compile failed: RuntimeError: regression-test-induced)")

    monkeypatch.setattr(compile_memory, "run_compile", fake_failure)

    # On CI there are no daily logs (gitignored for privacy), so
    # select_dailies returns [] and main() exits 0 before calling
    # run_compile. Monkeypatch select_dailies to return a fake path
    # INSIDE the vault root (compile_memory does relative_to(ROOT)
    # on each daily path for display).
    fake_daily = compile_memory.DAILY_DIR / "__test_fake__.md"
    monkeypatch.setattr(
        compile_memory,
        "select_dailies",
        lambda args, state: [fake_daily],
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
