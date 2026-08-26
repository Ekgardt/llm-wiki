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
from datetime import datetime
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
    # The hook reconfigures its stdout to UTF-8, so the reader has to say so
    # too: on Windows `text=True` alone decodes with the ANSI code page and
    # fails on the first non-ASCII character in the injected context.
    out = subprocess.check_output(
        [sys.executable, str(SCRIPT)],
        env=os.environ.copy(),
        text=True,
        encoding="utf-8",
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
    monkeypatch.setattr(session_start_context, "HOOK_STATE_LOCK_TIMEOUT", 120.0)

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
    monkeypatch.setattr(session_start_context, "HOOK_STATE_LOCK_TIMEOUT", 120.0)
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
    assert len(context.encode("utf-8")) <= (
        session_start_context.DEFAULT_CONTEXT_BUDGET.available_input_tokens
    )


def test_session_start_retains_3035_byte_advisory_under_shared_token_budget(monkeypatch):
    """A legacy byte cap must not discard context that fits the token budget."""
    import session_start_context

    advisory = "A" * 3035
    monkeypatch.setattr(session_start_context, "guardrails_block", lambda: "")
    monkeypatch.setattr(session_start_context, "metacognitive_block", lambda: "")
    monkeypatch.setattr(session_start_context, "advisory_block", lambda: advisory)
    monkeypatch.setattr(session_start_context, "_impact_block", lambda: "")
    monkeypatch.setattr(session_start_context, "health_block", lambda: "")
    # Force the index/daily/log to be tiny so the advisory alone is the
    # dominating section.
    monkeypatch.setattr(session_start_context, "trim_index", lambda *_: "")
    monkeypatch.setattr(session_start_context, "latest_daily", lambda: None)
    monkeypatch.setattr(session_start_context, "last_log_entries", lambda *_: "")

    context = session_start_context.build_context()

    assert advisory in context
    assert len(context.encode("utf-8")) <= 7680
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


def test_direct_session_start_routes_semantic_items_through_compiler(monkeypatch):
    import context_compiler
    import session_start_context

    captured = {}

    def compile_spy(items, *, budget, **packing):
        captured["items"] = tuple(items)
        captured["budget"] = budget
        captured["packing"] = packing
        return type("Packed", (), {"text": "compiled-session"})()

    monkeypatch.setattr(context_compiler, "compile_context_items", compile_spy)

    result = session_start_context._pack_session_sections([
        ("title", "# Project memory context"),
        ("guardrails", "safe"),
        ("health", "degraded"),
        ("advisory", "next action"),
        ("index", "evidence"),
        ("daily", "old event"),
    ])

    by_id = {item.item_id: item for item in captured["items"]}
    assert result == "compiled-session"
    assert captured["budget"] is session_start_context.DEFAULT_CONTEXT_BUDGET
    assert captured["packing"] == {}
    assert by_id["session:title"].priority_class == "evidence"
    assert by_id["session:title"].mandatory is False
    assert by_id["session:guardrails"].priority_class == "safety"
    assert by_id["session:guardrails"].mandatory is True
    assert by_id["session:health"].priority_class == "health"
    assert by_id["session:health"].mandatory is True
    assert by_id["session:advisory"].priority_class == "handoff"
    assert by_id["session:advisory"].mandatory is False
    assert by_id["session:index"].priority_class == "evidence"
    assert by_id["session:daily"].priority_class == "history"


def test_direct_session_start_drops_optional_sections_whole_under_pressure(monkeypatch):
    import session_start_context
    from context_budget import ContextBudget

    expected = "safety-whole\n\nhealth-whole"
    monkeypatch.setattr(
        session_start_context,
        "DEFAULT_CONTEXT_BUDGET",
        ContextBudget(None, len(expected.encode("utf-8")), 0, 0),
    )

    result = session_start_context._pack_session_sections([
        ("title", "title-must-drop-whole"),
        ("guardrails", "safety-whole"),
        ("health", "health-whole"),
        ("advisory", "advisory-must-drop-whole"),
        ("daily", "history-must-drop-whole"),
    ])

    assert result == expected
    assert "title" not in result
    assert "advisory" not in result
    assert "history" not in result


def test_session_start_heading_and_body_drop_as_one_complete_item(monkeypatch):
    from types import SimpleNamespace

    import session_start_context
    from context_budget import ContextBudget

    monkeypatch.setattr(session_start_context, "guardrails_block", lambda: "")
    monkeypatch.setattr(session_start_context, "metacognitive_block", lambda: "")
    monkeypatch.setattr(session_start_context, "health_block", lambda: "")
    monkeypatch.setattr(session_start_context, "advisory_block", lambda: "")
    monkeypatch.setattr(session_start_context, "_impact_block", lambda: "")
    monkeypatch.setattr(session_start_context, "trim_index", lambda text: "I" * 200)
    monkeypatch.setattr(
        session_start_context, "latest_daily", lambda: SimpleNamespace(name="x")
    )
    monkeypatch.setattr(session_start_context, "daily_excerpt", lambda path: "D" * 200)
    monkeypatch.setattr(session_start_context, "last_log_entries", lambda count: "L" * 200)
    monkeypatch.setattr(
        session_start_context,
        "DEFAULT_CONTEXT_BUDGET",
        ContextBudget(None, 23, 0, 0),
    )

    items = session_start_context.build_context_items()
    rendered = session_start_context._pack_session_items(items)

    assert "session:index_header" not in {item.item_id for item in items}
    assert "session:daily_header" not in {item.item_id for item in items}
    assert "session:log_header" not in {item.item_id for item in items}
    assert rendered == ""


def test_generated_project_context_routes_items_through_compiler(monkeypatch):
    import build_context

    captured = {}

    def compile_spy(items, *, budget, **packing):
        captured["items"] = tuple(items)
        captured["budget"] = budget
        captured["packing"] = packing
        return type("Packed", (), {"text": "compiled-project"})()

    monkeypatch.setattr(build_context, "compile_context_items", compile_spy)

    result = build_context._pack_project_context(
        [
            ("orientation", "## Project context: demo"),
            ("handoff", "### Where you left off\nresume here"),
            ("evidence", "## Evidence\nfact"),
            ("history", "## Recent activity\nold event"),
        ],
        2000,
    )

    assert result == "compiled-project"
    assert captured["budget"] is build_context.DEFAULT_CONTEXT_BUDGET
    assert captured["packing"] == {
        "emergency_byte_cap": 2000,
        "per_source_cap": 5,
        "per_parent_cap": 12,
    }
    by_text = {item.text: item for item in captured["items"]}
    assert by_text["## Project context: demo"].priority_class == "evidence"
    assert by_text["## Project context: demo"].mandatory is False
    assert by_text["### Where you left off\nresume here"].priority_class == "handoff"
    assert by_text["### Where you left off\nresume here"].mandatory is True
    assert by_text["## Evidence\nfact"].priority_class == "evidence"
    assert by_text["## Recent activity\nold event"].priority_class == "history"


def test_generated_project_context_drops_history_whole_under_pressure(monkeypatch):
    import build_context
    from context_budget import ContextBudget

    handoff = "### Where you left off\nresume-whole"
    monkeypatch.setattr(
        build_context,
        "DEFAULT_CONTEXT_BUDGET",
        ContextBudget(None, len(handoff.encode("utf-8")), 0, 0),
    )
    monkeypatch.setattr(build_context, "_read_state_handoff", lambda slug: "resume-whole")
    monkeypatch.setattr(build_context, "_find_project_pages", lambda slug: [])
    monkeypatch.setattr(
        build_context,
        "_find_recent_daily_activity",
        lambda slug: ["recent-history-must-drop-whole"],
    )
    monkeypatch.setattr(
        build_context,
        "load_state",
        lambda: {
            "codex_heartbeats": {
                "demo": {"reason": "last-seen-must-drop-whole", "at": "yesterday"}
            }
        },
    )

    result = build_context.build_context("demo")

    assert result == handoff
    assert "recent-history" not in result
    assert "last-seen" not in result
    assert "Project context" not in result


def test_recovered_handoff_routes_through_compiler_as_mandatory(monkeypatch):
    import context_compiler
    import integration_adapter
    import session_start_context

    captured = {}

    def compile_spy(items, *, budget, **packing):
        captured["items"] = tuple(items)
        captured["budget"] = budget
        captured["packing"] = packing
        return type("Packed", (), {"text": "compiled-recovery"})()

    monkeypatch.setattr(context_compiler, "compile_context_items", compile_spy)

    global_items = [
        item
        for item in (
            session_start_context._section_item("guardrails", "safe"),
            session_start_context._section_item("health", "degraded"),
            session_start_context._section_item("advisory", "next action"),
            session_start_context._section_item("daily", "old event"),
        )
        if item is not None
    ]
    result = integration_adapter._append_context(global_items, "recovered handoff")

    by_id = {item.item_id: item for item in captured["items"]}
    assert result == "compiled-recovery"
    assert captured["packing"] == {
        "emergency_byte_cap": captured["budget"].available_input_tokens,
    }
    assert by_id["session-start:project-handoff"].priority_class == "handoff"
    assert by_id["session-start:project-handoff"].mandatory is True
    assert by_id["session:advisory"].mandatory is False
    assert by_id["session:daily"].priority_class == "history"


def test_integration_uses_unpacked_project_handoff_items(monkeypatch):
    import context_compiler
    import integration_adapter
    from context_budget import ContextItem
    from project_journal import ProjectHandoffResult

    handoff_item = ContextItem(
        item_id="handoff:goal",
        text="## Active goal\n- Ship",
        source="project:demo",
        priority=2,
        relevance=0.8,
        confidence="high",
        freshness="fresh",
        token_cost=21,
        mandatory=False,
        representation="l1",
        parent_id="project:demo",
        priority_class="handoff",
    )
    monkeypatch.setattr(integration_adapter, "ProjectStore", lambda *args: object())
    monkeypatch.setattr(
        integration_adapter,
        "recover_project_handoff",
        lambda *args, **kwargs: ProjectHandoffResult(
            "prefiltered text must not be used", items=(handoff_item,)
        ),
    )
    captured = {}

    def compile_spy(items, *, budget, **packing):
        captured["items"] = tuple(items)
        return type("Packed", (), {"text": "one final pack"})()

    monkeypatch.setattr(context_compiler, "compile_context_items", compile_spy)

    handoff_items = integration_adapter._recover_project_handoff("demo", Path("demo"))
    rendered = integration_adapter._append_context([], handoff_items)

    assert handoff_items == (handoff_item,)
    assert captured["items"] == (handoff_item,)
    assert rendered == "one final pack"


def test_integration_keeps_complete_recovered_handoff_under_pressure(monkeypatch):
    import context_budget
    import integration_adapter
    import session_start_context
    from context_budget import ContextBudget
    from project_journal import ProjectProjection, build_handoff_items

    handoff_items = build_handoff_items(
        ProjectProjection(
            project="demo",
            goal={"goal": "Ship whole"},
            current_task={"task": "Test whole"},
            blockers={"blocker": "CI whole"},
            decisions={"decision": "Keep whole"},
            legacy_context="History whole",
            last_applied_sequence=5,
        )
    )
    expected = "\n\n".join(
        item.text
        for item in sorted(
            handoff_items,
            key=lambda item: (
                context_budget.PRIORITY_CLASS_ORDER[item.priority_class],
                item.item_id,
            ),
        )
    )
    monkeypatch.setattr(
        context_budget,
        "DEFAULT_CONTEXT_BUDGET",
        ContextBudget(None, len(expected.encode("utf-8")), 0, 0),
    )
    optional_history = session_start_context._section_item(
        "daily", "optional global history must drop"
    )
    assert optional_history is not None

    rendered = integration_adapter._append_context(
        [optional_history], handoff_items
    )

    assert rendered == expected
    assert "optional global history" not in rendered
    assert all(item.text in rendered for item in handoff_items)

    monkeypatch.setattr(
        context_budget,
        "DEFAULT_CONTEXT_BUDGET",
        ContextBudget(None, len(expected.encode("utf-8")) - 1, 0, 0),
    )
    failure = integration_adapter._append_context([], handoff_items)
    assert "mandatory_budget_exceeded" in failure
    assert "Ship whole" not in failure
    assert "History whole" not in failure


def test_integration_context_drops_optional_sections_whole_under_pressure(monkeypatch):
    import context_budget
    import integration_adapter
    import session_start_context
    from context_budget import ContextBudget

    expected = "safety-whole\n\nhealth-whole\n\nrecovered-whole"
    monkeypatch.setattr(
        context_budget,
        "DEFAULT_CONTEXT_BUDGET",
        ContextBudget(None, len(expected.encode("utf-8")), 0, 0),
    )
    items = [
        item
        for item in (
            session_start_context._section_item("guardrails", "safety-whole"),
            session_start_context._section_item("health", "health-whole"),
            session_start_context._section_item("advisory", "advisory-must-drop-whole"),
            session_start_context._section_item("daily", "history-must-drop-whole"),
        )
        if item is not None
    ]

    result = integration_adapter._append_context(items, "recovered-whole")

    assert result == expected
    assert "advisory" not in result
    assert "history" not in result


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
    import json

    import session_start_project_state

    result = session_start_project_state._clip("project-state-" * 100, 20)

    assert json.loads(result) == {"error": "budget"}
    assert len(result.encode("utf-8")) <= 20
    assert "project-state-project" not in result
    assert "truncated for hook injection" not in result


@pytest.mark.parametrize(
    ("platform", "root", "expected"),
    [
        ("win32", r"C:\workspace\project", True),
        ("win32", "C:/workspace/project", True),
        ("win32", "/workspace/project", False),
        ("win32", r"relative\project", False),
        ("linux", "/workspace/project", True),
        ("linux", r"C:\workspace\project", False),
        ("linux", "relative/project", False),
        ("linux", "/workspace/project\nforged", False),
        ("linux", "", False),
    ],
)
def test_project_root_requires_bounded_native_absolute_path(platform, root, expected):
    import session_start_project_state

    assert session_start_project_state._is_native_absolute_root(root, platform) is expected
    oversized = "x" * (session_start_project_state.MAX_PROJECT_ROOT_CHARS + 1)
    assert session_start_project_state._is_native_absolute_root(oversized, platform) is False


def test_project_dir_rejects_relative_env_before_resolution(monkeypatch):
    import session_start_project_state

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "relative/project")

    with pytest.raises(ValueError, match="native absolute"):
        session_start_project_state._resolve_project_dir()


def test_project_state_routes_mandatory_handoff_through_compiler(monkeypatch):
    import context_compiler
    import session_start_project_state

    captured = {}

    def compile_spy(items, *, budget, **packing):
        captured["items"] = tuple(items)
        captured["budget"] = budget
        captured["packing"] = packing
        return type("Packed", (), {"text": "compiled-project-state"})()

    monkeypatch.setattr(context_compiler, "compile_context_items", compile_spy)

    result = session_start_project_state._clip("project state", 2400)

    assert result == "compiled-project-state"
    assert captured["packing"] == {"emergency_byte_cap": 2400}
    assert len(captured["items"]) == 1
    item = captured["items"][0]
    assert item.text == "project state"
    assert item.priority_class == "handoff"
    assert item.mandatory is True


def test_project_handoff_alone_still_uses_shared_budget(monkeypatch):
    import context_budget
    import integration_adapter
    from context_budget import ContextBudget

    monkeypatch.setattr(
        context_budget, "DEFAULT_CONTEXT_BUDGET", ContextBudget(None, 10, 0, 0)
    )

    result = integration_adapter._append_context([], "handoff-content-too-large")

    assert "mandatory_budget_exceeded" in result
    assert "handoff-content-too-large" not in result


def test_integration_context_preserves_trailing_newline():
    import integration_adapter
    import session_start_context

    item = session_start_context._section_item("guardrails", "global")
    assert item is not None
    result = integration_adapter._append_context(
        [item], "handoff\n", trailing_newline=True
    )

    assert result == "global\n\nhandoff\n"


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


def _fake_section(monkeypatch, module, *, log_entry: str) -> None:
    """Silence every SessionStart section except the log tail."""
    monkeypatch.setattr(module, "guardrails_block", lambda: "")
    monkeypatch.setattr(module, "metacognitive_block", lambda: "")
    monkeypatch.setattr(module, "advisory_block", lambda: "")
    monkeypatch.setattr(module, "_impact_block", lambda: "")
    monkeypatch.setattr(module, "health_block", lambda: "")
    monkeypatch.setattr(module, "trim_index", lambda *_: "")
    monkeypatch.setattr(module, "latest_daily", lambda: None)
    monkeypatch.setattr(module, "last_log_entries", lambda *_: log_entry)


def test_session_context_stays_under_the_char_ceiling(monkeypatch):
    """An oversized low-priority section is dropped whole, not sliced."""
    import session_start_context

    _fake_section(monkeypatch, session_start_context, log_entry="L" * 9000)

    context = session_start_context.build_context()

    assert len(context) <= session_start_context.SESSION_CONTEXT_MAX_CHARS
    assert "# Project memory context" in context
    assert "LLLL" not in context


def test_session_context_keeps_sections_that_fit(monkeypatch):
    """Nothing is dropped while the payload stays under the ceiling."""
    import session_start_context

    _fake_section(monkeypatch, session_start_context, log_entry="- kept entry")

    context = session_start_context.build_context()

    assert "- kept entry" in context
    assert len(context) <= session_start_context.SESSION_CONTEXT_MAX_CHARS


def test_char_ceiling_never_drops_mandatory_sections():
    """Guard rails are mandatory: the ceiling may not evict them."""
    import session_start_context
    from context_budget import ContextItem

    def item(item_id: str, priority: int, size: int, mandatory: bool) -> ContextItem:
        return ContextItem(
            item_id=item_id,
            text="x" * size,
            source=f"test:{item_id}",
            priority=priority,
            relevance=1.0,
            confidence="high",
            freshness="fresh",
            token_cost=size,
            mandatory=mandatory,
            representation="l1",
            parent_id="session-start",
            priority_class="safety" if mandatory else "history",
        )

    items = [item("safety", 1, 500, True), item("history", 7, 5000, False)]

    rendered = session_start_context.fit_to_char_ceiling(
        items,
        lambda kept: "".join(entry.text for entry in kept),
        max_chars=1000,
    )

    assert len(rendered) == 500


def test_a_truncated_run_reports_only_that_it_was_truncated(monkeypatch):
    """Measured: the doctor wants 1.77s and gets 0.1. Seven checks never run,
    and two that do are cut short and report their state unreadable without
    marking themselves — the queue and the LSP, both fine at a real budget. So
    a truncated run's findings are discarded, not filtered."""
    import doctor
    import session_start_context

    report = {
        "overall_status": "error",
        "checks": [
            {"id": "queue", "status": "error", "details": {}},
            {
                "id": "scheduler",
                "status": "degraded",
                "details": {"budget_exhausted": True},
            },
        ],
    }
    monkeypatch.setattr(doctor, "run_doctor", lambda **kwargs: report)
    monkeypatch.setattr(
        doctor, "degraded_summary", lambda passed: "queue (error): unreadable"
    )

    block = session_start_context.health_block()

    assert "Health was not measured" in block
    assert "1 of 2 checks" in block
    assert "queue" not in block
    assert "unreadable" not in block


def test_a_complete_run_reports_its_findings(monkeypatch):
    import doctor
    import session_start_context

    report = {
        "overall_status": "degraded",
        "checks": [{"id": "index", "status": "degraded", "details": {}}],
    }
    monkeypatch.setattr(doctor, "run_doctor", lambda **kwargs: report)
    monkeypatch.setattr(doctor, "degraded_summary", lambda passed: "index: stale")

    assert session_start_context.health_block() == "## Health\n\nindex: stale\n\n"


def _knowledge_state(monkeypatch, state: dict) -> str:
    import session_start_context

    monkeypatch.setattr(session_start_context, "_load_state_safe", lambda: state)
    return session_start_context.metacognitive_block()


def test_a_compile_that_failed_today_is_not_reported_as_fresh(monkeypatch):
    """Only a committed compile makes the memory fresh."""
    block = _knowledge_state(
        monkeypatch,
        {
            "last_compile_finished_at": datetime.now().isoformat(timespec="seconds"),
            "last_compile_status": "error",
        },
    )

    assert "fresh (today)" not in block
    assert "failed" in block


def test_a_vault_that_never_committed_a_compile_says_so(monkeypatch):
    """A finished-but-failed run is not a compile that ever happened."""
    block = _knowledge_state(
        monkeypatch,
        {
            "last_compile_finished_at": datetime.now().isoformat(timespec="seconds"),
            "last_compile_status": "error",
        },
    )

    assert "never" in block


def test_a_committed_compile_today_is_still_fresh(monkeypatch):
    """The line the fix must not break."""
    block = _knowledge_state(
        monkeypatch,
        {
            "last_compile_at": datetime.now().isoformat(timespec="seconds"),
            "last_compile_status": "ok",
        },
    )

    assert "fresh (today)" in block


def test_compile_backlog_reads_the_utc_stamp_compile_actually_writes():
    """`compile_memory._utc_now` writes `...Z`; SessionStart must survive it.

    Parsed, a `Z` suffix is an aware datetime. It was subtracted from a naive
    `datetime.now()`, so every SessionStart on a vault that had ever compiled
    raised `TypeError: can't subtract offset-naive and offset-aware datetimes`
    and the adapter recorded a lost capture instead of injecting context.
    Measured on the live vault on 2026-08-26: `last_compile_at` held
    `2026-08-26T14:49:32.401938Z` and `adapter_session_start` held exactly that
    TypeError.
    """
    import session_start_context

    aware = session_start_context._compile_backlog_days(
        {"last_compile_at": "2026-08-26T14:49:32.401938Z"}
    )
    naive = session_start_context._compile_backlog_days(
        {"last_compile_at": "2026-08-26T14:49:32.401938"}
    )

    assert isinstance(aware, int)
    assert aware >= 0
    assert aware == naive
    assert session_start_context._compile_backlog_days({}) is None
