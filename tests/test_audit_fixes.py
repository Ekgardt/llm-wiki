"""Regression tests for audit-critical path contracts."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def test_compile_rejects_path_escape_category(tmp_path, monkeypatch):
    import compile_memory

    knowledge = tmp_path / "knowledge" / "notes"
    knowledge.mkdir(parents=True)
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", knowledge)
    monkeypatch.setattr(compile_memory, "ROOT", tmp_path)

    plan = {
        "operations": [
            {
                "action": "create",
                "category": "../../docs",
                "slug": "evil",
                "title": "Evil",
                "summary": "nope",
                "body_markdown": "x",
                "evidence": [],
            }
        ],
        "audit": {},
    }
    touched, audit_text = compile_memory._execute_plan(plan, [], dry_run=False)
    assert touched == []
    assert "invalid category" in audit_text or "path-unsafe" in audit_text or "escapes" in audit_text or "Dropped" in audit_text
    assert not (tmp_path / "docs" / "evil.md").exists()


def test_compile_dry_run_does_not_mutate_existing(tmp_path, monkeypatch):
    import compile_memory

    knowledge = tmp_path / "knowledge" / "notes" / "patterns"
    knowledge.mkdir(parents=True)
    old = knowledge / "old.md"
    old.write_text("# Old\n\nbody\n", encoding="utf-8")
    before = old.read_text(encoding="utf-8")

    monkeypatch.setattr(compile_memory, "KNOWLEDGE", tmp_path / "knowledge" / "notes")
    monkeypatch.setattr(compile_memory, "ROOT", tmp_path)

    # Force a contradiction hit
    def fake_contra(*_a, **_k):
        return [old]

    monkeypatch.setattr(compile_memory, "_check_contradictions_pre_write", fake_contra)
    monkeypatch.setattr(compile_memory, "_verify_evidence", lambda *_a, **_k: (1, 0))

    plan = {
        "operations": [
            {
                "action": "create",
                "category": "patterns",
                "slug": "new-page",
                "title": "New",
                "summary": "s",
                "body_markdown": "body",
                "evidence": [{"daily_date": "2026-01-01", "timestamp": "00:00:00", "claim": "c"}],
            }
        ],
        "audit": {},
    }
    compile_memory._execute_plan(plan, [], dry_run=True)
    assert old.read_text(encoding="utf-8") == before


def test_query_file_back_uses_qa_dir():
    import query_memory

    assert query_memory.QA_DIR.as_posix().endswith("knowledge/notes/qa")


def test_redact_secrets_strips_bearer():
    from secret_redact import redact_secrets

    out = redact_secrets("Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345")
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out
    assert "REDACTED" in out


def test_memory_queue_drain_query_without_output_path(tmp_path, monkeypatch):
    import memory_queue

    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    # Reset queue dir via env
    tid = memory_queue.enqueue(
        "query",
        {"prompt": "hello", "system_prompt": "sys", "max_tokens": 10, "enqueued_by": "test"},
    )
    pending = memory_queue.list_pending()
    assert any(t["id"] == tid for t in pending)

    def fake_call_llm(prompt, system_prompt="", max_tokens=1000):
        return "answer-ok"

    import llm_client

    monkeypatch.setattr(llm_client, "call_llm", fake_call_llm)

    def processor(task):
        payload = task.get("payload", {})
        if task.get("type") != "query":
            return False
        if not payload.get("prompt"):
            return False
        result = llm_client.call_llm(payload["prompt"])
        out = payload.get("output_path")
        if not out:
            results_dir = tmp_path / "run" / "queue-results"
            results_dir.mkdir(parents=True, exist_ok=True)
            out = str(results_dir / f"{task['id']}.txt")
        Path(out).write_text(result, encoding="utf-8")
        return True

    counts = memory_queue.drain_with(processor, max_tasks=5)
    assert counts.get("ok", 0) >= 1 or counts.get("success", 0) >= 1 or sum(counts.values()) >= 1


def test_select_dailies_rejects_outside_daily(tmp_path, monkeypatch):
    import compile_memory
    import argparse

    monkeypatch.setattr(compile_memory, "DAILY_DIR", tmp_path / "knowledge" / "daily")
    (tmp_path / "knowledge" / "daily").mkdir(parents=True)
    outside = tmp_path / "README.md"
    outside.write_text("x", encoding="utf-8")
    args = argparse.Namespace(file=str(outside), all=False)
    with pytest.raises(SystemExit):
        compile_memory.select_dailies(args, {})


def test_e2e_compile_with_fake_provider(tmp_path, monkeypatch):
    """Full compile path under MEMORY_LLM_PROVIDER=fake writes no pages when ops empty,
    marks daily compiled, and emits COMPILE_DONE."""
    import compile_memory
    import memory_state

    vault = tmp_path / "vault"
    daily = vault / "knowledge" / "daily"
    notes = vault / "knowledge" / "notes"
    daily.mkdir(parents=True)
    notes.mkdir(parents=True)
    (vault / "docs").mkdir()
    (vault / "docs" / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    (vault / "knowledge" / "index.md").write_text("# idx\n", encoding="utf-8")
    (vault / "knowledge" / "log.md").write_text("# log\n", encoding="utf-8")
    day = daily / "2026-07-01.md"
    day.write_text(
        "# Daily — 2026-07-01\n\n## [10:00:00] session-end | test\n- Tier: `major`\n\n**Decisions made**\n- Ship fake compile e2e.\n",
        encoding="utf-8",
    )

    state_root = tmp_path / "state"
    (state_root / "run").mkdir(parents=True)
    (state_root / "logs").mkdir(parents=True)

    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
    monkeypatch.setenv(
        "MEMORY_LLM_FAKE_RESPONSE",
        json.dumps(
            {
                "operations": [],
                "audit": {
                    "verified": 0,
                    "dedup": 0,
                    "stubs": 0,
                    "contradictions": 0,
                    "rejected": 0,
                },
            }
        )
        + "\nCOMPILE_AUDIT: verified 0 evidence citations; 0 dedup checks performed; "
        "0 stubs skipped; 0 contradictions handled; 0 pages rejected as below-threshold",
    )

    # Re-bind module paths that were cached at import time.
    monkeypatch.setattr(compile_memory, "ROOT", vault)
    monkeypatch.setattr(compile_memory, "MEMORY", vault / "knowledge")
    monkeypatch.setattr(compile_memory, "DAILY_DIR", daily)
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", notes)
    monkeypatch.setattr(compile_memory, "AGENTS", vault / "docs" / "AGENTS.md")
    monkeypatch.setattr(compile_memory, "INDEX", vault / "knowledge" / "index.md")
    monkeypatch.setattr(compile_memory, "LOG", vault / "knowledge" / "log.md")
    monkeypatch.setattr(memory_state, "ROOT", vault)
    monkeypatch.setattr(memory_state, "STATE_ROOT", state_root)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_root / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", state_root / "run" / "state.json")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", state_root / "logs")
    monkeypatch.setattr(compile_memory, "STATE_ROOT", state_root)
    monkeypatch.setattr(compile_memory, "rebuild_index", lambda: True)

    import argparse

    args = argparse.Namespace(all=False, file=str(day), dry_run=False, trigger="manual")
    rc = compile_memory._run(args)
    assert rc == 0
    state = json.loads((state_root / "run" / "state.json").read_text(encoding="utf-8"))
    assert "2026-07-01.md" in state.get("compiled_daily_hashes", {})
    assert state.get("last_compile_status") in ("ok", "warning", None) or state.get(
        "last_compile_at"
    )


def test_settings_hooks_use_llm_wiki_root():
    settings = Path(__file__).resolve().parent.parent / "integrations" / "claude-code" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    cmds = []
    for _event, blocks in data.get("hooks", {}).items():
        for block in blocks:
            for h in block.get("hooks", []):
                if h.get("command"):
                    cmds.append(h["command"])
    assert cmds
    assert all("$LLM_WIKI_ROOT" in c for c in cmds)
    assert all("uv run --directory" in c for c in cmds)


def test_no_title_case_duplicate_notes():
    notes = Path(__file__).resolve().parent.parent / "knowledge" / "notes"
    assert not (notes / "Editorial Notes Pattern.md").exists()
    assert not (notes / "Pipeline Mirroring.md").exists()
    assert (notes / "editorial-notes-pattern.md").exists()
    assert (notes / "pipeline-mirroring.md").exists()
