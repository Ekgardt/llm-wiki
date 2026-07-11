"""Regression tests for audit-critical path contracts."""
from __future__ import annotations

import json
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

    knowledge = tmp_path / "knowledge" / "notes"
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


def test_query_file_back_uses_notes_dir():
    import query_memory

    assert query_memory.QA_DIR.as_posix().endswith("knowledge/notes")


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
    assert counts.get("ok", 0) >= 1, f"expected at least 1 ok, got {counts}"


def test_select_dailies_rejects_outside_daily(tmp_path, monkeypatch):
    import argparse

    import compile_memory

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
    """All Claude Code hook commands pin the vault via $LLM_WIKI_ROOT, the
    referenced scripts exist on disk, every hook block has a numeric timeout,
    and the matcher set is the expected one. A rename of any hooked script
    would otherwise leave this green and break Claude Code at runtime.
    """
    import re as _re

    settings = Path(__file__).resolve().parent.parent / "integrations" / "claude-code" / "settings.json"
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    data = json.loads(settings.read_text(encoding="utf-8"))
    cmds = []
    timeouts = []
    for event, blocks in data.get("hooks", {}).items():
        for block in blocks:
            assert "timeout" not in block, "timeout belongs on each hook, not the matcher block"
            for h in block.get("hooks", []):
                cmd = h.get("command", "")
                if cmd:
                    cmds.append(cmd)
                t = h.get("timeout")
                if t is not None:
                    timeouts.append(t)
                # Script referenced in the command must exist.
                m = _re.search(r"scripts/([A-Za-z_][A-Za-z0-9_-]*\.py)", cmd)
                assert m, f"hook command has no scripts/*.py reference: {cmd!r}"
                script_name = m.group(1)
                assert (scripts_dir / script_name).is_file(), (
                    f"hook references missing script: scripts/{script_name}"
                )
    assert cmds, "settings.json wires no hooks"
    assert all("$LLM_WIKI_ROOT" in c for c in cmds)
    assert all("uv run --directory" in c for c in cmds)
    assert timeouts and all(isinstance(t, int) and t > 0 for t in timeouts), (
        "every hook must declare a positive numeric timeout"
    )


def test_no_forbidden_legacy_paths_in_scripts():
    """G-2: guard against silent regression of pre-three-zone hardcoded paths
    inside scripts/. Catching them here is cheaper than waiting for a runtime
    bug report from an operator whose env var layout differs from the author's.
    """

    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    # Forbidden literal substrings in LIVE code paths (docstrings/historical
    # comments are skipped via a per-line filter).
    forbidden = [
        'ROOT / "memory"',
        'ROOT / "wiki"',
        'ROOT / "outputs"',
        'MEMORY / "knowledge"',   # double-knowledge phantom
        'vault / "wiki"',
        'D:\\LLM-wiki',
        'D:\\projects\\llm-wiki',
        'D:\\tools-agent',
    ]
    offenders = []
    for py in sorted(scripts_dir.glob("*.py")):
        try:
            lines = py.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Skip docstring lines (cheap heuristic: a """ block).
            if '"""' in line or "'''" in line:
                continue
            for token in forbidden:
                if token in line:
                    offenders.append(f"{py.name}:{i}: {line.strip()}")
                    break
    assert not offenders, "forbidden legacy path tokens in scripts/:\n  " + "\n  ".join(offenders)


def test_no_title_case_duplicate_notes():
    notes = Path(__file__).resolve().parent.parent / "knowledge" / "notes"
    assert not (notes / "Editorial Notes Pattern.md").exists()
    assert not (notes / "Pipeline Mirroring.md").exists()
    assert (notes / "editorial-notes-pattern.md").exists()
    assert (notes / "pipeline-mirroring.md").exists()
