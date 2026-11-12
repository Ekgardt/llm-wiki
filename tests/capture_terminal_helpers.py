"""Helpers shared by test_capture_terminal.py and the test files importing them."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import flush_memory
import llm_client
import pytest
from reliable_memory import canonical_json_bytes, publish_runtime_file, sha256_bytes


@pytest.fixture(autouse=True)
def _own_session_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the session record these tests provoke out of the owner's vault.

    `process_new_capture` keeps the session before anything judges it, and it
    writes to `flush_memory.ROOT` — a module-level constant, not the vault the
    coordinator under test is bound to. This checkout has been the live vault
    since 2026-08-21, so every run left a record in it; two of them were still
    there on 2026-08-26, under `2026-08-16` and today's date, invented sessions
    named `session-1` with bodies reading `debug evidence` and `status only`.
    The writer never raises by contract, so nothing said so.

    Pointing `ROOT` at a directory outside the vault is enough: the transaction
    refuses a target outside its own vault and the writer swallows that, which
    is exactly the no-op these tests always assumed they were getting.
    """
    vault = tmp_path / "session-record-vault"
    (vault / "knowledge" / "raw" / "sessions").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    return vault


def _ready_intent_binding(queue, coordinator, registry, text: str):
    evidence = [{"role": "transcript", "parts": [{"type": "text", "text": text}]}]
    source = {
        "source_occurrence_id": "occurrence-1",
        "source_event_id": "event-1",
        "occurred_at": None,
        "host": "opencode",
        "event": "session_end",
        "session": "session-1",
        "project_slug": "project-1",
        "worktree": None,
        "trigger": "session-end",
        "checkpoint_reason": None,
        "chunk_index": 0,
        "chunk_count": 1,
        "evidence": evidence,
    }
    complete_digest = sha256_bytes(canonical_json_bytes(source))
    chunk_digest = sha256_bytes(canonical_json_bytes(evidence))
    identity = {
        "schema_version": "capture-intent/v1",
        "source_occurrence_id": source["source_occurrence_id"],
        "source_event_id": source["source_event_id"],
        "occurred_at": source["occurred_at"],
        "checkpoint_reason": source["checkpoint_reason"],
        "chunk_index": source["chunk_index"],
        "chunk_sha256": chunk_digest,
    }
    intent_id = sha256_bytes(canonical_json_bytes(identity))
    record = {
        "schema_version": "capture-intent/v1",
        "intent_id": intent_id,
        **source,
        "complete_input_sha256": complete_digest,
        "chunk_sha256": chunk_digest,
    }
    encoded = canonical_json_bytes(record)
    pending = f"run/capture-intents/pending/{intent_id[:2]}/{intent_id}.json"
    ready = f"run/capture-intents/ready/{intent_id[:2]}/{intent_id}.json"
    (queue.state_root / ready).parent.mkdir(parents=True)
    queue.index_capture_intent_pending(
        intent_id=intent_id,
        pending_path=pending,
        ready_path=ready,
        intent_sha256=sha256_bytes(encoded),
        byte_size=len(encoded),
    )
    publish_runtime_file(
        queue.state_root / ready,
        encoded,
        state_root=queue.state_root,
        create_only=True,
    )
    queue.mark_capture_intent_ready(
        intent_id=intent_id,
        pending_path=pending,
        ready_path=ready,
        intent_sha256=sha256_bytes(encoded),
        byte_size=len(encoded),
    )
    owner = registry.acquire("capture", scope=f"intent:{intent_id}")
    fence = coordinator.acquire_intent_fence(intent_id, mode="capture", owner=owner)
    binding = queue.enqueue_capture_task_replay_safe(
        "flush",
        1,
        {
            "intent_id": intent_id,
            "intent_path": ready,
            "intent_sha256": sha256_bytes(encoded),
        },
        intent_id=intent_id,
        intent_path=ready,
        intent_sha256=sha256_bytes(encoded),
        capture_fence=fence,
        owner=owner,
    )
    coordinator.release_intent_fence(fence)
    registry.release(owner)
    return binding


class _FakeNoContentProvider:
    def __init__(self, response: str = "FLUSH_OK") -> None:
        self.descriptor = llm_client.provider_candidates("fake", max_tokens=1500)[0]
        self.calls: list[str] = []
        self.response = response

    def __call__(self, prompt: str, system_prompt: str, max_tokens: int):
        self.calls.append(prompt)
        assert system_prompt
        assert max_tokens == 1500
        return llm_client.LLMResult(
            self.descriptor,
            self.response,
            True,
            None,
            "prompt",
        )


class _NoContentProcessor:
    def __init__(self, queue, coordinator, provider) -> None:
        self.queue = queue
        self.coordinator = coordinator
        self.provider = provider

    def __call__(self, lease, active, task_fence, intent_fence, owner):
        return flush_memory.process_new_capture(
            self.queue,
            self.coordinator,
            lease,
            active,
            task_fence,
            intent_fence,
            owner,
            llm_call=self.provider,
        )


class _TimedCaptureProcessor(_NoContentProcessor):
    def __init__(self, queue, coordinator, provider, chosen_at: datetime) -> None:
        super().__init__(queue, coordinator, provider)
        self.chosen_at = chosen_at

    def __call__(self, lease, active, task_fence, intent_fence, owner):
        return flush_memory.process_new_capture(
            self.queue,
            self.coordinator,
            lease,
            active,
            task_fence,
            intent_fence,
            owner,
            llm_call=self.provider,
            now=lambda: self.chosen_at,
        )


def _expire_capture_lease(queue, task_id: str) -> None:
    """Make the crashed task claimable again, whichever way the crash left it.

    A crash used to abandon the lease, and this helper's old assertion pinned
    that: recovery had to find exactly one expired lease. Since 2026-08-27 the
    worker settles its claim on failure (`processor_failed`, immediate retry),
    so a crashed task is usually already `ready` and recovery finds nothing.
    Since 2026-09-14 a `ready` retry waits a backoff
    (`docs/research/2026-09-14-the-small-integrity-gaps.md`); the wait is moved to the past.
    """
    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            "UPDATE tasks SET lease_expires_at=? WHERE id=? AND state='leased'",
            ("2000-01-01T00:00:00+00:00", task_id),
        )
        database.execute(
            "UPDATE tasks SET available_at=? WHERE id=? AND state='ready'",
            ("2000-01-01T00:00:00+00:00", task_id),
        )
    queue.recover_expired_leases()
    with sqlite3.connect(queue.db_path) as database:
        state = database.execute(
            "SELECT state FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
    assert state == "ready", f"task must be claimable again, found {state!r}"


def _crash_before_ledger(*_args, **_kwargs):
    raise RuntimeError("injected decision ledger crash")


def _assert_completed_capture(queue, binding, relative) -> None:
    assert relative == f"run/queue-results/capture-{binding.intent_id}.json"
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT state,result_reference FROM tasks WHERE id=?", (binding.task_id,)
        ).fetchone() == ("succeeded", relative)
        assert database.execute(
            "SELECT COUNT(*) FROM semantic_decisions WHERE intent_id=?",
            (binding.intent_id,),
        ).fetchone() == (1,)


def _assert_major_daily_file_once(path: Path, intent_id: str) -> None:
    content = path.read_text(encoding="utf-8")
    assert content.count("- **Decisions made** - keep this") == 1
    assert content.count(f"- Capture intent: `{intent_id}`") == 1
