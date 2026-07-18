"""Regression test: session_start_context strips machine-specific noise (Round 3 #I4).

Injected additionalContext MUST NOT include:
  - `Trigger: ...`  (hook metadata)
  - `Transcript: ...` (local filesystem paths)
  - `Project root: ...` (absolute paths, machine-specific)
  - Session-end header UUIDs (literal session IDs)

Useful signal must survive:
  - `# Session Memory Index` header
  - Wikilinks into knowledge/notes/
  - `Project slug: ...` (project identity, useful)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "session_start_context.py"


@pytest.fixture(scope="module")
def injected_context() -> str:
    # conftest.py bootstraps LLM_WIKI_ROOT and LLM_WIKI_STATE_ROOT in
    # os.environ; subprocess inherits. The script otherwise depends on
    # memory_state.ROOT (script-file-relative), which works regardless,
    # so this subprocess is safe even without env vars — but tests that
    # DO rely on env stay consistent.
    import os
    out = subprocess.check_output(
        [sys.executable, str(SCRIPT)],
        env=os.environ.copy(),
        text=True,
    )
    d = json.loads(out)
    return d["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize(
    "forbidden",
    [
        "- Trigger:",
        "- Transcript:",
        "- Project root:",
        r"C:\Users\\",
    ],
)
def test_noise_stripped(injected_context: str, forbidden: str):
    assert forbidden not in injected_context, (
        f"injected context still contains forbidden fragment: {forbidden!r}"
    )


def test_no_session_uuid(injected_context: str):
    """Session-end headers should have their `| <uuid>` tail trimmed."""
    uuid_re = re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    )
    assert not uuid_re.search(injected_context), (
        "UUID found in injected context — session-id strip regex regressed"
    )


def test_useful_signal_preserved(injected_context: str):
    """Useful signal must survive the shared-budget pack.

    Task 14 contract: SessionStart packs sections by priority under one
    shared ContextBudget. In a bloated checkout (many guardrails, many
    stale-page impact entries) the lower-priority sections (index/daily/log)
    may be dropped whole. The test asserts the bare minimum that must
    ALWAYS survive: the title header plus at least one substantive block
    (guardrails, metacognitive, health, or — when budget allows — the
    knowledge index).
    """
    assert "# Project memory context" in injected_context
    substantive = (
        "## Guard rails" in injected_context
        or "## Your knowledge state" in injected_context
        or "## Health" in injected_context
        or "Session Memory Index" in injected_context
    )
    assert substantive, "no substantive SessionStart block survived packing"


def test_context_size_reasonable(injected_context: str):
    """Sanity: injected context fits in a reasonable budget (≤ 4 KB)."""
    assert 0 < len(injected_context) <= 4000, (
        f"injected context size outside expected range: {len(injected_context)} chars"
    )


def test_latest_daily_ignores_readme(tmp_path, monkeypatch):
    import session_start_context

    (tmp_path / "2026-07-12.md").write_text("daily", encoding="utf-8")
    (tmp_path / "2026-07-13-notes.md").write_text("notes", encoding="utf-8")
    (tmp_path / "9999-99-99.md").write_text("invalid", encoding="utf-8")
    (tmp_path / "README.md").write_text("help", encoding="utf-8")
    monkeypatch.setattr(session_start_context, "DAILY_DIR", tmp_path)

    assert session_start_context.latest_daily().name == "2026-07-12.md"


def test_nightly_catchup_claim_is_atomic_and_once_per_date(tmp_path, monkeypatch):
    import memory_state
    import session_start_context

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(session_start_context, "update_state", memory_state.update_state)
    monkeypatch.setattr(session_start_context, "HOOK_STATE_LOCK_TIMEOUT", 10.0)

    with ThreadPoolExecutor(max_workers=16) as pool:
        claims = list(
            pool.map(
                lambda _: session_start_context._claim_nightly_catchup("2026-07-12"),
                range(32),
            )
        )

    assert claims.count(True) == 1
    state = json.loads(memory_state.STATE_FILE.read_text(encoding="utf-8"))
    assert state["nightly_catchup_claim"]["date"] == "2026-07-12"
    assert state["nightly_catchup_claim"]["status"] == "claimed"
    assert state["nightly_catchup_claim"]["expires_at"]
    memory_state.update_state(lambda value: value.update(last_nightly_date="2026-07-13"))
    assert session_start_context._claim_nightly_catchup("2026-07-13") is False


def test_expired_nightly_claim_can_be_retried(tmp_path, monkeypatch):
    import memory_state
    import session_start_context

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(session_start_context, "update_state", memory_state.update_state)
    monkeypatch.setattr(session_start_context, "HOOK_STATE_LOCK_TIMEOUT", 10.0)
    memory_state.save_state({
        "nightly_catchup_claim": {
            "date": "2026-07-12",
            "status": "claimed",
            "claimed_at": "2026-07-12T01:00:00",
            "expires_at": "2026-07-12T01:30:00",
        }
    })

    assert session_start_context._claim_nightly_catchup(
        "2026-07-12", now="2026-07-12T02:00:00"
    ) is True


def test_session_start_spawns_scheduled_nightly_nonblocking_only_for_catchup(
    monkeypatch, tmp_path
):
    import session_start_context

    spawned = []
    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
    monkeypatch.setattr(session_start_context, "ROOT", tmp_path)
    monkeypatch.setattr(session_start_context, "_claim_nightly_catchup", lambda today=None: True)
    monkeypatch.setattr(
        session_start_context, "spawn_detached", lambda args: spawned.append(args) or 321
    )

    session_start_context._maybe_spawn_nightly_catchup("2026-07-12")

    assert spawned == [[
        sys.executable,
        str(tmp_path / "scripts" / "scheduled_nightly.py"),
    ]]


def test_nightly_catchup_claim_returns_quickly_when_state_lock_is_held(
    tmp_path, monkeypatch
):
    import memory_state
    import session_start_context

    state_dir = tmp_path / "run"
    lock_file = state_dir / "state.json.lock"
    state_dir.mkdir()
    lock_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_dir / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", lock_file)
    monkeypatch.setattr(session_start_context, "update_state", memory_state.update_state)

    started = time.perf_counter()
    claimed = session_start_context._claim_nightly_catchup("2026-07-13")

    assert claimed is False
    assert time.perf_counter() - started < 0.75


def test_failed_nightly_spawn_releases_quickly_when_state_lock_is_held(
    tmp_path, monkeypatch
):
    import memory_state
    import session_start_context

    state_dir = tmp_path / "run"
    lock_file = state_dir / "state.json.lock"
    state_dir.mkdir()
    lock_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(memory_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(memory_state, "STATE_FILE", state_dir / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", lock_file)
    monkeypatch.setattr(session_start_context, "update_state", memory_state.update_state)
    monkeypatch.setattr(session_start_context, "_claim_nightly_catchup", lambda today: True)
    monkeypatch.setattr(session_start_context, "spawn_detached", lambda args: None)
    monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)

    started = time.perf_counter()
    session_start_context._maybe_spawn_nightly_catchup("2026-07-13")

    assert time.perf_counter() - started < 0.75


def test_session_start_omits_health_when_doctor_is_healthy(monkeypatch):
    import doctor
    import session_start_context

    monkeypatch.setattr(doctor, "run_doctor", lambda **kwargs: {"overall_status": "ok"})
    monkeypatch.setattr(doctor, "degraded_summary", lambda report: "")

    assert session_start_context.health_block() == ""


def test_session_start_injects_bounded_health_only_when_degraded(monkeypatch):
    import doctor
    import session_start_context

    monkeypatch.setattr(
        doctor,
        "run_doctor",
        lambda **kwargs: {"overall_status": "degraded", "checks": []},
    )
    monkeypatch.setattr(doctor, "degraded_summary", lambda report: "index: stale")
    monkeypatch.setattr(session_start_context, "guardrails_block", lambda: "")
    monkeypatch.setattr(session_start_context, "metacognitive_block", lambda: "")
    monkeypatch.setattr(session_start_context, "advisory_block", lambda: "")
    monkeypatch.setattr(session_start_context, "_impact_block", lambda: "")

    assert session_start_context.health_block() == "## Health\n\nindex: stale\n\n"
    context = session_start_context.build_context()
    assert "## Health" in context
    assert len(context) <= session_start_context.MAX_CONTEXT_CHARS


def test_session_start_pack_respects_shared_budget_and_never_slices_items(monkeypatch):
    """Task 14: build_context routes through the shared ContextBudget.

    A section larger than the emergency cap must be dropped whole — its
    tail must not appear truncated mid-item.
    """
    import session_start_context

    large = "X" * (session_start_context.MAX_CONTEXT_CHARS * 3)
    monkeypatch.setattr(session_start_context, "guardrails_block", lambda: "")
    monkeypatch.setattr(session_start_context, "metacognitive_block", lambda: "")
    monkeypatch.setattr(session_start_context, "advisory_block", lambda: large)
    monkeypatch.setattr(session_start_context, "_impact_block", lambda: "")
    monkeypatch.setattr(session_start_context, "health_block", lambda: "")
    # Force the index/daily/log to be tiny so the advisory alone is the
    # dominating section.
    monkeypatch.setattr(session_start_context, "trim_index", lambda *_: "")
    monkeypatch.setattr(session_start_context, "latest_daily", lambda: None)
    monkeypatch.setattr(session_start_context, "last_log_entries", lambda *_: "")

    context = session_start_context.build_context()

    assert len(context) <= session_start_context.MAX_CONTEXT_CHARS
    # The over-large advisory must NOT be present at all (dropped whole),
    # never partially sliced into the output.
    assert "X" * 100 not in context
    assert "… (truncated)" not in context


def test_session_start_budget_constant_is_a_context_budget():
    import session_start_context
    from context_budget import ContextBudget

    assert isinstance(
        session_start_context.DEFAULT_CONTEXT_BUDGET, ContextBudget
    )
    assert session_start_context.DEFAULT_CONTEXT_BUDGET.available_input_tokens > 0


def test_session_and_project_context_use_the_same_budget_contract():
    import build_context
    import session_start_context

    assert build_context.DEFAULT_CONTEXT_BUDGET is session_start_context.DEFAULT_CONTEXT_BUDGET


def test_session_start_impossible_mandatory_budget_is_visible_not_sliced(monkeypatch):
    import session_start_context
    from context_budget import ContextBudget

    monkeypatch.setattr(session_start_context, "DEFAULT_CONTEXT_BUDGET", ContextBudget(None, 10, 0, 0))

    result = session_start_context._pack_session_sections(
        [("guardrails", "mandatory-guardrail-content")]
    )

    assert "mandatory_budget_exceeded" in result
    assert "mandatory-guardrail-content" not in result
    assert "truncated" not in result


def test_project_state_impossible_cap_is_visible_not_character_sliced():
    import session_start_project_state

    result = session_start_project_state._clip("project-state-" * 100, 20)

    assert "mandatory_emergency_cap_exceeded" in result
    assert "project-state-project" not in result
    assert "truncated for hook injection" not in result


def test_project_handoff_alone_still_uses_shared_budget(monkeypatch):
    import context_budget
    import integration_adapter
    from context_budget import ContextBudget

    monkeypatch.setattr(
        context_budget, "DEFAULT_CONTEXT_BUDGET", ContextBudget(None, 10, 0, 0)
    )

    result = integration_adapter._append_context("", "handoff-content-too-large")

    assert "mandatory_budget_exceeded" in result
    assert "handoff-content-too-large" not in result


def test_session_start_health_fails_open(monkeypatch):
    import doctor
    import session_start_context

    monkeypatch.setattr(
        doctor,
        "run_doctor",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("doctor unavailable")),
    )

    assert session_start_context.health_block() == ""


def test_session_start_health_uses_strict_doctor_budget(monkeypatch):
    import doctor
    import session_start_context

    received = {}

    def run(**kwargs):
        received.update(kwargs)
        return {"overall_status": "ok", "checks": []}

    monkeypatch.setattr(doctor, "run_doctor", run)
    monkeypatch.setattr(doctor, "degraded_summary", lambda report: "")

    session_start_context.health_block()

    assert 0 < received["time_budget_seconds"] <= 0.1


def test_session_start_recovers_transactions_before_health_context(monkeypatch):
    import session_start_context

    events = []
    monkeypatch.setattr(
        session_start_context,
        "_recover_transactions",
        lambda: events.append("recover"),
    )
    monkeypatch.setattr(
        session_start_context,
        "build_context",
        lambda: events.append("context") or "context",
    )
    monkeypatch.setattr(session_start_context, "_maybe_spawn_nightly_catchup", lambda: None)
    monkeypatch.setattr(session_start_context, "latest_daily", lambda: None)
    monkeypatch.setattr(session_start_context, "write_debug", lambda *args: None)
    monkeypatch.setattr("sys.argv", ["session_start_context.py", "--output-file", "out"])
    monkeypatch.setattr(Path, "write_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: None)

    assert session_start_context.main() == 0
    assert events == ["recover", "context"]


def test_session_start_recovery_rejects_symlinked_database(tmp_path, monkeypatch):
    import session_start_context

    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    (root / "knowledge/notes").mkdir(parents=True)
    run = state_root / "run"
    run.mkdir(parents=True)
    external = tmp_path / "outside.sqlite3"
    external.write_bytes(b"outside")
    try:
        (run / "markdown-transactions.sqlite3").symlink_to(external)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    called = []
    monkeypatch.setattr(session_start_context, "ROOT", root)
    monkeypatch.setattr(session_start_context, "STATE_ROOT", state_root)
    monkeypatch.setattr(
        "markdown_transaction.MarkdownCoordinator.recover",
        lambda *args, **kwargs: called.append(kwargs),
    )

    session_start_context._recover_transactions()

    assert called == []


def test_session_start_recovery_passes_hard_limit_and_deadline(tmp_path, monkeypatch):
    import session_start_context

    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    (root / "knowledge/notes").mkdir(parents=True)
    run = state_root / "run"
    run.mkdir(parents=True)
    database = run / "markdown-transactions.sqlite3"
    database.write_bytes(b"safe")
    received = []
    monkeypatch.setattr(session_start_context, "ROOT", root)
    monkeypatch.setattr(session_start_context, "STATE_ROOT", state_root)
    monkeypatch.setattr(
        session_start_context,
        "validate_runtime_file",
        lambda *args, **kwargs: True,
        raising=False,
    )
    monkeypatch.setattr(
        "markdown_transaction.MarkdownCoordinator.__init__",
        lambda self, *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "markdown_transaction.MarkdownCoordinator.recover",
        lambda self, **kwargs: received.append(kwargs),
    )

    started = time.monotonic()
    session_start_context._recover_transactions()

    assert received[0]["max_transactions"] > 0
    assert started < received[0]["deadline"] <= started + 0.11


def test_session_start_health_latency_is_bounded_with_large_unsafe_queue(
    tmp_path, monkeypatch
):
    import session_start_context

    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "scripts").mkdir()
    queue = state_root / "run" / "queue"
    queue.mkdir(parents=True)
    (state_root / "logs").mkdir()
    (state_root / "cache").mkdir()
    for number in range(250):
        (queue / f"{number:04}.json").write_bytes(b"x" * 70_000)
    external = tmp_path / "outside.json"
    external.write_text('{"payload":"secret"}', encoding="utf-8")
    try:
        (queue / "unsafe.json").symlink_to(external)
    except (OSError, NotImplementedError):
        pass
    monkeypatch.setattr(session_start_context, "ROOT", root)
    monkeypatch.setattr(session_start_context, "STATE_ROOT", state_root)

    started = time.perf_counter()
    block = session_start_context.health_block()
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert block.startswith("## Health")
    assert "secret" not in block
