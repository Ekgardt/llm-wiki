from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import flush_memory  # noqa: E402
import llm_client  # noqa: E402
import memory_queue  # noqa: E402
import operational_ownership  # noqa: E402
from reliable_memory import (  # noqa: E402
    canonical_json_bytes,
    publish_runtime_file,
    sha256_bytes,
)

from tests.test_queue_v3_capture_links import (  # noqa: E402
    _capture_binding,
    _coordinator,
    _queue,
)


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
    monkeypatch.setattr(flush_memory, "ROOT", vault)
    return vault


def _publish_decision(
    queue, coordinator, binding, lease, owner, task_fence, intent_fence
):
    key = sha256_bytes(
        canonical_json_bytes({"intent_id": binding.intent_id, "stage": "flush"})
    )
    relative = f"run/queue-results/capture-decision-{key}.json"
    data = canonical_json_bytes(
        {"schema_version": "capture-decision/v1", "tier": "ok"}
    )
    publish_runtime_file(
        queue.state_root / relative,
        data,
        state_root=queue.state_root,
        create_only=True,
    )
    return queue.publish_semantic_decision(
        coordinator,
        task_id=lease.id,
        intent_id=binding.intent_id,
        stage="flush",
        decision_path=relative,
        decision_sha256=sha256_bytes(data),
        active_link_digest=binding.active_digest,
        task_fence=task_fence,
        intent_fence=intent_fence,
        owner=owner,
    )


def _terminal_bytes(binding, decision) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": "capture-terminal/v1",
            "intent_id": binding.intent_id,
            "intent_sha256": binding.intent_sha256,
            "semantic_decisions": [
                {
                    "stage": decision.stage,
                    "decision_path": decision.decision_path,
                    "decision_sha256": decision.decision_sha256,
                }
            ],
            "processing_binding": {
                "kind": "task",
                "task_id": binding.task_id,
                "active_link_digest": binding.active_digest,
            },
            "disposition": {
                "kind": "no_durable_content",
                "decision_sha256": decision.decision_sha256,
            },
        }
    )


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


def _publish_terminal_file(
    queue, coordinator, binding, lease, owner, task_fence, intent_fence
):
    decision = _publish_decision(
        queue,
        coordinator,
        binding,
        lease,
        owner,
        task_fence,
        intent_fence,
    )
    terminal = _terminal_bytes(binding, decision)
    relative = f"run/queue-results/capture-{binding.intent_id}.json"
    publish_runtime_file(
        queue.state_root / relative,
        terminal,
        state_root=queue.state_root,
        create_only=True,
    )
    return relative, terminal


def _stage_capture_terminal(
    queue, coordinator, binding, lease, owner, *, scope: str
) -> str:
    with queue.queue_owner(
        role="queue-worker", scope=scope, parent=owner
    ), memory_queue.capture_task_fences(
        queue,
        coordinator,
        binding.task_id,
        intent_id=binding.intent_id,
        mode="worker",
        owner=owner,
    ) as (task_fence, intent_fence):
        assert intent_fence is not None
        relative, _terminal = _publish_terminal_file(
            queue,
            coordinator,
            binding,
            lease,
            owner,
            task_fence,
            intent_fence,
        )
    return relative


def _expire_capture_lease(queue, task_id: str) -> None:
    """Make the crashed task claimable again, whichever way the crash left it.

    A crash used to abandon the lease, and this helper's old assertion pinned
    that: recovery had to find exactly one expired lease. Since 2026-08-27 the
    worker settles its claim on failure (`processor_failed`, immediate retry),
    so a crashed task is usually already `ready` and recovery finds nothing.
    """
    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            "UPDATE tasks SET lease_expires_at=? WHERE id=? AND state='leased'",
            ("2000-01-01T00:00:00+00:00", task_id),
        )
    queue.recover_expired_leases()
    with sqlite3.connect(queue.db_path) as database:
        state = database.execute(
            "SELECT state FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
    assert state == "ready", f"task must be claimable again, found {state!r}"


def _crash_before_terminal(*_args):
    raise RuntimeError("injected terminal publication crash")


def _crash_before_ledger(*_args, **_kwargs):
    raise RuntimeError("injected decision ledger crash")


def _require_value(value):
    assert value is not None
    return value


def _assert_terminal_completion(queue, binding, result, relative, terminal) -> None:
    assert result == relative
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            """SELECT state,result_reference,result_sha256,result_operation_id
               FROM tasks WHERE id=?""",
            (binding.task_id,),
        ).fetchone() == (
            "succeeded",
            relative,
            sha256_bytes(terminal),
            f"capture-terminal:{binding.intent_id}",
        )
        assert database.execute(
            "SELECT outcome,error_code FROM attempt_history WHERE task_id=?",
            (binding.task_id,),
        ).fetchall() == [("succeeded", None)]


def _assert_provider_calls(provider, expected: int) -> None:
    assert len(provider.calls) == expected


def _assert_capture_decision(queue, binding) -> None:
    decision_key = sha256_bytes(
        canonical_json_bytes({"intent_id": binding.intent_id, "stage": "flush"})
    )
    decision_path = queue.results_dir / f"capture-decision-{decision_key}.json"
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    assert decision["provider"] == {
        "candidate_index": 0,
        "fallback_from": [],
        "model": "fake-v1",
        "provider": "fake",
        "structured_output": "prompt",
    }
    assert decision["outcome"] == "semantic_ok"
    assert decision["operation_plan"] == []


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


def _assert_worker_idle(queue, coordinator, processor) -> None:
    assert flush_memory.run_capture_worker_once(
        queue,
        coordinator,
        process_missing=processor,
    ) is None


def _assert_decision_count(queue, intent_id: str, expected: int) -> None:
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT COUNT(*) FROM semantic_decisions WHERE intent_id=?",
            (intent_id,),
        ).fetchone() == (expected,)


def _assert_terminal_absent(queue, intent_id: str) -> None:
    assert not (queue.results_dir / f"capture-{intent_id}.json").exists()


def _assert_decision_file_exists(queue, intent_id: str) -> None:
    decision_key = sha256_bytes(
        canonical_json_bytes({"intent_id": intent_id, "stage": "flush"})
    )
    assert (queue.results_dir / f"capture-decision-{decision_key}.json").is_file()


def _assert_terminal_result(relative, binding, provider) -> None:
    assert relative == f"run/queue-results/capture-{binding.intent_id}.json"
    _assert_provider_calls(provider, 1)


def _assert_major_daily_file(path: Path, intent_id: str) -> None:
    content = path.read_text(encoding="utf-8")
    assert "## [12:34:56] session-end | session-1" in content
    assert "- **Decisions made** - keep this" in content
    assert f"- Capture intent: `{intent_id}`" in content


def _assert_major_daily_file_once(path: Path, intent_id: str) -> None:
    content = path.read_text(encoding="utf-8")
    assert content.count("- **Decisions made** - keep this") == 1
    assert content.count(f"- Capture intent: `{intent_id}`") == 1


def _assert_minor_daily_file(path: Path, intent_id: str) -> None:
    content = path.read_text(encoding="utf-8")
    assert "- **Gotchas / debugging** - keep this" in content
    assert f"- Capture intent: `{intent_id}`" in content
    assert "- Tier: `minor`" in content


def _assert_markdown_terminal(queue, binding, relative) -> dict[str, object]:
    terminal = json.loads((queue.state_root / relative).read_text(encoding="utf-8"))
    disposition = terminal["disposition"]
    assert disposition["kind"] == "markdown_committed"
    assert disposition["outputs"][0]["path"] == "knowledge/daily/2026-08-16.md"
    assert disposition["decision_sha256"] == terminal["semantic_decisions"][0][
        "decision_sha256"
    ]
    return disposition


def _assert_committed_capture_transaction(coordinator, disposition) -> None:
    with sqlite3.connect(coordinator.database_path) as database:
        assert database.execute(
            'SELECT state,operation_id FROM "transaction" WHERE id=?',
            (disposition["transaction_id"],),
        ).fetchone() == ("committed", disposition["operation_id"])
        assert database.execute(
            'SELECT path,after_hash FROM "operation" WHERE transaction_id=?',
            (disposition["transaction_id"],),
        ).fetchone() == (
            disposition["outputs"][0]["path"],
            disposition["outputs"][0]["sha256"],
        )


def _completed_capture_for_purge(tmp_path: Path):
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _ready_intent_binding(queue, coordinator, registry, "status only")
    provider = _FakeNoContentProvider()
    processor = _NoContentProcessor(queue, coordinator, provider)
    terminal_relative = _require_value(
        flush_memory.run_capture_worker_once(
            queue,
            coordinator,
            process_missing=processor,
        )
    )
    with sqlite3.connect(queue.db_path) as database:
        intent_relative = database.execute(
            "SELECT relative_path FROM capture_intents WHERE intent_id=?",
            (binding.intent_id,),
        ).fetchone()[0]
        decision_relative = database.execute(
            "SELECT decision_path FROM semantic_decisions WHERE intent_id=?",
            (binding.intent_id,),
        ).fetchone()[0]
        database.execute(
            "UPDATE tasks SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (binding.task_id,),
        )
    return (
        queue,
        binding,
        tmp_path / intent_relative,
        tmp_path / decision_relative,
        tmp_path / terminal_relative,
    )


def test_terminal_file_is_bound_before_task_success(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _capture_binding(
        queue,
        coordinator,
        registry,
        intent_id="1" * 64,
        intent_sha256="2" * 64,
    )
    lease = _require_value(queue.claim("capture-worker"))
    owner = registry.acquire("queue-worker", scope="worker:capture-terminal")
    queue.results_dir.mkdir()

    with queue.queue_owner(
        role="queue-worker", scope="worker:capture-terminal", parent=owner
    ), memory_queue.capture_task_fences(
        queue,
        coordinator,
        binding.task_id,
        intent_id=binding.intent_id,
        mode="worker",
        owner=owner,
    ) as (task_fence, intent_fence):
        _require_value(intent_fence)
        relative, terminal = _publish_terminal_file(
            queue,
            coordinator,
            binding,
            lease,
            owner,
            task_fence,
            intent_fence,
        )
        result = queue.complete_capture_terminal(
            lease,
            intent_id=binding.intent_id,
            terminal_path=relative,
            terminal_sha256=sha256_bytes(terminal),
            active_link_digest=binding.active_digest,
            task_fence=task_fence,
            intent_fence=intent_fence,
            owner=owner,
        )

    _assert_terminal_completion(queue, binding, result, relative, terminal)
    registry.release(owner)


def test_terminal_completion_rejects_lost_intent_fence(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _capture_binding(
        queue,
        coordinator,
        registry,
        intent_id="3" * 64,
        intent_sha256="4" * 64,
    )
    lease = queue.claim("capture-worker")
    assert lease is not None
    owner = registry.acquire("queue-worker", scope="worker:capture-terminal")
    queue.results_dir.mkdir()

    with queue.queue_owner(
        role="queue-worker", scope="worker:capture-terminal", parent=owner
    ):
        task_fence = queue.acquire_task_fence(
            binding.task_id, mode="worker", owner=owner
        )
        intent_fence = coordinator.acquire_intent_fence(
            binding.intent_id, mode="worker", owner=owner
        )
        decision = _publish_decision(
            queue,
            coordinator,
            binding,
            lease,
            owner,
            task_fence,
            intent_fence,
        )
        terminal = _terminal_bytes(binding, decision)
        relative = f"run/queue-results/capture-{binding.intent_id}.json"
        publish_runtime_file(
            tmp_path / relative,
            terminal,
            state_root=tmp_path,
            create_only=True,
        )
        coordinator.release_intent_fence(intent_fence)

        with pytest.raises(memory_queue.QueueOperationError, match="intent_fence_lost"):
            queue.complete_capture_terminal(
                lease,
                intent_id=binding.intent_id,
                terminal_path=relative,
                terminal_sha256=sha256_bytes(terminal),
                active_link_digest=binding.active_digest,
                task_fence=task_fence,
                intent_fence=intent_fence,
                owner=owner,
            )

        queue.release_task_fence(task_fence)

    registry.release(owner)


def test_preexisting_terminal_completes_before_provider_call(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _capture_binding(
        queue,
        coordinator,
        registry,
        intent_id="5" * 64,
        intent_sha256="6" * 64,
    )
    lease = queue.claim("capture-worker")
    assert lease is not None
    owner = registry.acquire("queue-worker", scope="worker:capture-terminal")
    queue.results_dir.mkdir()
    provider_calls: list[str] = []

    relative = _stage_capture_terminal(
        queue,
        coordinator,
        binding,
        lease,
        owner,
        scope="worker:capture-terminal",
    )
    with queue.queue_owner(
        role="queue-worker", scope="worker:capture-terminal", parent=owner
    ):
        result = flush_memory.process_capture_lease(
            queue,
            coordinator,
            lease,
            owner=owner,
            process_missing=lambda *_args: provider_calls.append("called"),
        )

    assert result == relative
    assert provider_calls == []
    registry.release(owner)


def test_capture_worker_claims_v3_terminal_without_provider_call(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _capture_binding(
        queue,
        coordinator,
        registry,
        intent_id="7" * 64,
        intent_sha256="8" * 64,
    )
    lease = queue.claim("crashed-worker")
    assert lease is not None
    owner = registry.acquire("queue-worker", scope="worker:stage-terminal")
    queue.results_dir.mkdir()

    relative = _stage_capture_terminal(
        queue,
        coordinator,
        binding,
        lease,
        owner,
        scope="worker:stage-terminal",
    )

    registry.release(owner)
    _expire_capture_lease(queue, binding.task_id)
    provider_calls: list[str] = []

    result = flush_memory.run_capture_worker_once(
        queue,
        coordinator,
        process_missing=lambda *_args: provider_calls.append("called"),
    )

    assert result == relative
    assert provider_calls == []


def test_capture_worker_publishes_no_content_terminal_once(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _ready_intent_binding(queue, coordinator, registry, "status only")
    provider = _FakeNoContentProvider()
    processor = _NoContentProcessor(queue, coordinator, provider)

    relative = flush_memory.run_capture_worker_once(
        queue,
        coordinator,
        process_missing=processor,
    )

    _assert_completed_capture(queue, binding, relative)
    _assert_provider_calls(provider, 1)
    _assert_capture_decision(queue, binding)
    _assert_worker_idle(queue, coordinator, processor)
    _assert_provider_calls(provider, 1)


def test_capture_worker_reuses_decision_after_terminal_publish_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _ready_intent_binding(queue, coordinator, registry, "status only")
    provider = _FakeNoContentProvider()
    processor = _NoContentProcessor(queue, coordinator, provider)

    real_publish_terminal = flush_memory._publish_no_content_terminal
    monkeypatch.setattr(
        flush_memory, "_publish_no_content_terminal", _crash_before_terminal
    )
    with pytest.raises(RuntimeError, match="injected terminal publication crash"):
        flush_memory.run_capture_worker_once(
            queue,
            coordinator,
            process_missing=processor,
        )

    _assert_provider_calls(provider, 1)
    _assert_terminal_absent(queue, binding.intent_id)
    _assert_decision_count(queue, binding.intent_id, 1)
    _expire_capture_lease(queue, binding.task_id)
    monkeypatch.setattr(
        flush_memory, "_publish_no_content_terminal", real_publish_terminal
    )

    relative = flush_memory.run_capture_worker_once(
        queue,
        coordinator,
        process_missing=processor,
    )

    _assert_terminal_result(relative, binding, provider)


def test_capture_worker_indexes_decision_file_after_ledger_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _ready_intent_binding(queue, coordinator, registry, "status only")
    provider = _FakeNoContentProvider()
    processor = _NoContentProcessor(queue, coordinator, provider)

    real_publish_decision = queue.publish_semantic_decision
    monkeypatch.setattr(queue, "publish_semantic_decision", _crash_before_ledger)
    with pytest.raises(RuntimeError, match="injected decision ledger crash"):
        flush_memory.run_capture_worker_once(
            queue,
            coordinator,
            process_missing=processor,
        )

    _assert_decision_file_exists(queue, binding.intent_id)
    _assert_provider_calls(provider, 1)
    _assert_decision_count(queue, binding.intent_id, 0)
    _expire_capture_lease(queue, binding.task_id)
    monkeypatch.setattr(queue, "publish_semantic_decision", real_publish_decision)

    relative = flush_memory.run_capture_worker_once(
        queue,
        coordinator,
        process_missing=processor,
    )

    _assert_terminal_result(relative, binding, provider)


def test_capture_worker_commits_major_daily_block_and_terminal(tmp_path: Path) -> None:
    (tmp_path / "knowledge" / "daily").mkdir(parents=True)
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _ready_intent_binding(queue, coordinator, registry, "decision evidence")
    provider = _FakeNoContentProvider(
        "FLUSH_MAJOR\n- **Decisions made** - keep this"
    )
    chosen_at = datetime(2026, 8, 16, 12, 34, 56, tzinfo=timezone.utc)
    processor = _TimedCaptureProcessor(queue, coordinator, provider, chosen_at)

    relative = flush_memory.run_capture_worker_once(
        queue,
        coordinator,
        process_missing=processor,
    )

    _assert_completed_capture(queue, binding, relative)
    _assert_provider_calls(provider, 1)
    _assert_major_daily_file(
        tmp_path / "knowledge" / "daily" / "2026-08-16.md", binding.intent_id
    )
    disposition = _assert_markdown_terminal(queue, binding, relative)
    _assert_committed_capture_transaction(coordinator, disposition)


def test_capture_worker_reuses_markdown_after_terminal_publish_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daily_path = tmp_path / "knowledge" / "daily" / "2026-08-16.md"
    daily_path.parent.mkdir(parents=True)
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _ready_intent_binding(queue, coordinator, registry, "decision evidence")
    provider = _FakeNoContentProvider(
        "FLUSH_MAJOR\n- **Decisions made** - keep this"
    )
    chosen_at = datetime(2026, 8, 16, 12, 34, 56, tzinfo=timezone.utc)
    processor = _TimedCaptureProcessor(queue, coordinator, provider, chosen_at)
    real_publish_terminal = flush_memory._publish_capture_terminal
    monkeypatch.setattr(
        flush_memory, "_publish_capture_terminal", _crash_before_terminal
    )

    with pytest.raises(RuntimeError, match="injected terminal publication crash"):
        flush_memory.run_capture_worker_once(
            queue,
            coordinator,
            process_missing=processor,
        )

    _assert_provider_calls(provider, 1)
    _assert_terminal_absent(queue, binding.intent_id)
    _assert_major_daily_file_once(daily_path, binding.intent_id)
    _expire_capture_lease(queue, binding.task_id)
    monkeypatch.setattr(
        flush_memory, "_publish_capture_terminal", real_publish_terminal
    )

    relative = flush_memory.run_capture_worker_once(
        queue,
        coordinator,
        process_missing=processor,
    )

    _assert_terminal_result(relative, binding, provider)
    _assert_major_daily_file_once(daily_path, binding.intent_id)


def test_capture_worker_commits_minor_daily_block_and_terminal(tmp_path: Path) -> None:
    daily_path = tmp_path / "knowledge" / "daily" / "2026-08-16.md"
    daily_path.parent.mkdir(parents=True)
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _ready_intent_binding(queue, coordinator, registry, "debug evidence")
    provider = _FakeNoContentProvider(
        "FLUSH_MINOR\n- **Gotchas / debugging** - keep this"
    )
    chosen_at = datetime(2026, 8, 16, 12, 34, 56, tzinfo=timezone.utc)
    processor = _TimedCaptureProcessor(queue, coordinator, provider, chosen_at)

    relative = flush_memory.run_capture_worker_once(
        queue,
        coordinator,
        process_missing=processor,
    )

    _assert_completed_capture(queue, binding, relative)
    _assert_provider_calls(provider, 1)
    _assert_minor_daily_file(daily_path, binding.intent_id)
    disposition = _assert_markdown_terminal(queue, binding, relative)
    _assert_committed_capture_transaction(coordinator, disposition)


def test_capture_worker_rejects_noncanonical_provider_output(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    binding = _ready_intent_binding(queue, coordinator, registry, "status only")
    provider = _FakeNoContentProvider("FLUSH_OK\ntrailing content")
    processor = _NoContentProcessor(queue, coordinator, provider)

    with pytest.raises(RuntimeError, match="invalid flush output"):
        flush_memory.run_capture_worker_once(
            queue,
            coordinator,
            process_missing=processor,
        )

    _assert_provider_calls(provider, 1)
    _assert_terminal_absent(queue, binding.intent_id)
    _assert_decision_count(queue, binding.intent_id, 0)


def _assert_sources_retained(
    intent_path: Path, decision_path: Path, terminal_path: Path
) -> None:
    assert intent_path.is_file()
    assert decision_path.is_file()
    assert terminal_path.is_file()


def _assert_manifest_archives_both_sources(
    export: Path, tmp_path: Path, intent_path: Path, decision_path: Path
) -> None:
    manifest = json.loads((export / "manifest.json").read_bytes())
    archived = manifest["capture_artifacts"]
    assert {item["source_path"] for item in archived} == {
        intent_path.relative_to(tmp_path).as_posix(),
        decision_path.relative_to(tmp_path).as_posix(),
    }
    assert all((export / item["archive_path"]).is_file() for item in archived)


def _assert_capture_tables(
    queue, *, tasks: int, intents: int, decisions: int
) -> None:
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute("SELECT COUNT(*) FROM tasks").fetchone() == (tasks,)
        assert database.execute(
            "SELECT COUNT(*) FROM capture_intents"
        ).fetchone() == (intents,)
        assert database.execute(
            "SELECT COUNT(*) FROM semantic_decisions"
        ).fetchone() == (decisions,)


def _assert_sources_purged_terminal_retained(
    intent_path: Path, decision_path: Path, terminal_path: Path
) -> None:
    assert not intent_path.exists()
    assert not decision_path.exists()
    assert terminal_path.is_file()


def _assert_purge_authorizations(queue, count: int) -> None:
    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT COUNT(*) FROM task_purge_authorizations"
        ).fetchone() == (count,)


def test_ordinary_purge_removes_terminal_proven_capture_sources_but_retains_terminal(
    tmp_path: Path,
) -> None:
    queue, binding, intent_path, decision_path, terminal_path = (
        _completed_capture_for_purge(tmp_path)
    )
    export = tmp_path / "exports" / "capture"

    receipt = queue.purge(
        terminal_before=datetime(2001, 1, 1, tzinfo=timezone.utc),
        export_path=export,
    )

    assert receipt == memory_queue.PurgeReceipt(1, (binding.task_id,))
    _assert_capture_tables(queue, tasks=0, intents=0, decisions=0)
    _assert_sources_purged_terminal_retained(
        intent_path, decision_path, terminal_path
    )
    assert (export / "results" / f"{binding.task_id}.result").is_file()


def test_ordinary_purge_rejects_capture_terminal_without_exact_task_binding(
    tmp_path: Path,
) -> None:
    queue, binding, intent_path, decision_path, terminal_path = (
        _completed_capture_for_purge(tmp_path)
    )
    with sqlite3.connect(queue.db_path) as database:
        database.execute(
            "UPDATE tasks SET result_operation_id='forged' WHERE id=?",
            (binding.task_id,),
        )
    export = tmp_path / "exports" / "unbound-capture"

    with pytest.raises(
        memory_queue.QueueOperationError, match="capture_terminal_unbound"
    ):
        queue.purge(
            terminal_before=datetime(2001, 1, 1, tzinfo=timezone.utc),
            export_path=export,
        )

    with sqlite3.connect(queue.db_path) as database:
        assert database.execute(
            "SELECT state FROM tasks WHERE id=?", (binding.task_id,)
        ).fetchone() == ("succeeded",)
    _assert_capture_tables(queue, tasks=1, intents=1, decisions=1)
    _assert_sources_retained(intent_path, decision_path, terminal_path)
    assert not export.exists()


def test_ordinary_purge_resumes_after_export_publication_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, binding, intent_path, decision_path, terminal_path = (
        _completed_capture_for_purge(tmp_path)
    )
    export = tmp_path / "exports" / "published-crash"

    def crash_before_commit(*_args, **_kwargs):
        raise RuntimeError("injected crash before purge commit")

    monkeypatch.setattr(queue, "_commit_ordinary_purge", crash_before_commit)
    with pytest.raises(RuntimeError, match="injected crash before purge commit"):
        queue.purge(
            terminal_before=datetime(2001, 1, 1, tzinfo=timezone.utc),
            export_path=export,
        )

    assert export.is_dir()
    _assert_sources_retained(intent_path, decision_path, terminal_path)
    resumed = memory_queue.MemoryQueue._from_v3_candidate(
        queue.db_path, state_root=tmp_path
    )

    receipt = resumed.purge(
        terminal_before=datetime(2001, 1, 1, tzinfo=timezone.utc),
        export_path=export,
    )

    assert receipt == memory_queue.PurgeReceipt(1, (binding.task_id,))
    _assert_sources_purged_terminal_retained(
        intent_path, decision_path, terminal_path
    )
    assert (export / "purge-receipt.json").is_file()


def test_ordinary_purge_resumes_partial_cleanup_after_database_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, binding, intent_path, decision_path, terminal_path = (
        _completed_capture_for_purge(tmp_path)
    )
    export = tmp_path / "exports" / "cleanup-crash"

    def crash_during_cleanup(_records, evidence):
        assert len(evidence) == 1
        intent_path.unlink()
        raise RuntimeError("injected crash during purge cleanup")

    monkeypatch.setattr(queue, "_cleanup_ordinary_purge_artifacts", crash_during_cleanup)
    with pytest.raises(RuntimeError, match="injected crash during purge cleanup"):
        queue.purge(
            terminal_before=datetime(2001, 1, 1, tzinfo=timezone.utc),
            export_path=export,
        )

    with sqlite3.connect(queue.db_path) as database:
        assert database.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
    _assert_purge_authorizations(queue, 1)
    _assert_manifest_archives_both_sources(
        export, tmp_path, intent_path, decision_path
    )

    resumed = memory_queue.MemoryQueue._from_v3_candidate(
        queue.db_path, state_root=tmp_path
    )
    receipt = resumed.purge(
        terminal_before=datetime(2001, 1, 1, tzinfo=timezone.utc),
        export_path=export,
    )

    assert receipt == memory_queue.PurgeReceipt(1, (binding.task_id,))
    _assert_purge_authorizations(queue, 0)
    _assert_sources_purged_terminal_retained(
        intent_path, decision_path, terminal_path
    )
    assert (export / "purge-receipt.json").is_file()


def test_a_flush_body_that_ends_in_a_newline_is_kept(tmp_path: Path) -> None:
    """A trailing newline used to destroy the capture it was attached to.

    Nine sessions on the live vault were lost under `noncanonical flush
    output`. The wire grammar is still closed: the tier token must open the
    output, and a body that is only whitespace is still refused.
    """
    assert flush_memory._parse_capture_wire_output("FLUSH_MAJOR\nkept\n") == (
        "major",
        "kept",
    )
    assert flush_memory._parse_capture_wire_output(
        "FLUSH_MINOR\n  spaced out  "
    ) == ("minor", "spaced out")

    with pytest.raises(RuntimeError, match="empty flush body"):
        flush_memory._parse_capture_wire_output("FLUSH_MAJOR\n   \n  ")

    with pytest.raises(RuntimeError, match="invalid flush output"):
        flush_memory._parse_capture_wire_output("here you go\nFLUSH_MAJOR\nbody")
