"""A temporary vault whose capture queue is adopted, with the adapter pointed at it."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def session_intent_payload(seed: bytes) -> tuple[str, bytes]:
    """A real, schema-valid retained v1 session intent for lifecycle fixtures."""
    from integration_adapter import _encoded_capture_record

    identity = seed.decode()
    document, payload = _encoded_capture_record({
        "source_occurrence_id": identity, "source_event_id": identity,
        "occurred_at": "2026-09-29T00:00:00+00:00", "host": "codex",
        "event": "session_end", "session": identity, "project_slug": None,
        "worktree": None, "trigger": None, "checkpoint_reason": None,
        "chunk_index": 0, "chunk_count": 1,
        "evidence": [{"role": "transcript", "parts": [{"type": "text", "text": identity}]}],
    })
    return str(document["intent_id"]), payload


def adopted_capture_vault(tmp_path, monkeypatch, adapter):
    """(state root, project directory) of a fresh adopted vault."""
    from installed_memory_repair import repair_installed_vault

    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    project = tmp_path / "project"
    (vault / "knowledge/projects").mkdir(parents=True)
    (vault / "scripts").mkdir()
    (vault / "scripts/integration_adapter.py").write_bytes(
        (SCRIPTS_DIR / "integration_adapter.py").read_bytes()
    )
    project.mkdir()
    report = repair_installed_vault(
        root=vault,
        state_root=state_root,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )
    if report["overall_status"] != "ok":
        raise AssertionError(report)
    monkeypatch.setattr(adapter, "ROOT", vault)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setattr(adapter, "STATE_ROOT", state_root)
    return state_root, project


def host_transcript(state_root: Path, name: str, text: str) -> Path:
    """A transcript under the one allowed root a test owns."""
    path = state_root / "cache" / "transient-transcripts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def published_intents(state_root: Path) -> list[Path]:
    return sorted((state_root / "run/capture-intents/ready").glob("*/*.json"))


def intent_records(state_root: Path) -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in published_intents(state_root)
    ]


def intent_summaries(state_root: Path) -> list[tuple]:
    """(event, trigger, session) of every published intent."""
    return [
        (record["event"], record["trigger"], record["session"])
        for record in intent_records(state_root)
    ]


def capture_task_count(state_root: Path) -> int:
    with sqlite3.connect(state_root / "run/queue-v3.sqlite3") as database:
        return database.execute("SELECT COUNT(*) FROM capture_task_links").fetchone()[0]
