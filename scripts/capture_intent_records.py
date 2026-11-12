"""Records that bind a capture intent to its Markdown commit and terminal result."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from llm_wiki.capture_preconditions import capture_transaction_preconditions

# Basis unknown: value predates measurement; review when a terminal record is refused for size.
MAX_CAPTURE_TERMINAL_BYTES = 64 * 1024


_CAPTURE_SOURCE_FIELDS = (
    "source_occurrence_id",
    "source_event_id",
    "occurred_at",
    "host",
    "event",
    "session",
    "project_slug",
    "worktree",
    "trigger",
    "checkpoint_reason",
    "chunk_index",
    "chunk_count",
    "evidence",
)


def _require_bound_intent(intent_id: object, intent_sha256: object) -> None:
    """The binding names both halves of the intent identity, or neither."""
    if not isinstance(intent_id, str) or not isinstance(intent_sha256, str):
        raise RuntimeError("capture task binding is invalid")


def capture_intent_reference(
    lease: object, active: object
) -> tuple[str, str, str]:
    payload = getattr(lease, "payload", None)
    intent_id = getattr(active, "intent_id", None)
    intent_sha256 = getattr(active, "intent_sha256", None)
    if not isinstance(payload, Mapping):
        raise RuntimeError("capture task payload is invalid")
    _require_bound_intent(intent_id, intent_sha256)
    intent_path = f"run/capture-intents/ready/{intent_id[:2]}/{intent_id}.json"
    actual = (
        set(payload),
        payload.get("intent_id"),
        payload.get("intent_sha256"),
        isinstance(payload.get("intent_path"), str),
        getattr(active, "task_id", None),
    )
    expected = (
        {"intent_id", "intent_path", "intent_sha256"},
        intent_id,
        intent_sha256,
        True,
        getattr(lease, "id", None),
    )
    if actual != expected:
        raise RuntimeError("capture task payload conflicts with its binding")
    return intent_id, intent_path, intent_sha256


def decode_capture_intent(data: bytes) -> dict[str, object]:
    from llm_wiki.formats.canonical import canonical_json_bytes
    from llm_wiki.formats.schema import validate_schema

    try:
        record = json.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("capture intent JSON is invalid") from exc
    if not isinstance(record, dict):
        raise RuntimeError("capture intent must be a JSON object")
    validate_schema(record, Path(__file__).with_name("schemas") / "capture-intent-v1.json")
    if canonical_json_bytes(record) != data:
        raise RuntimeError("capture intent is not canonical JSON")
    return record


def require_capture_intent_identity(record: Mapping[str, object]) -> None:
    from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes

    source = {field: record[field] for field in _CAPTURE_SOURCE_FIELDS}
    chunk_sha256 = sha256_bytes(canonical_json_bytes(record["evidence"]))
    complete_sha256 = sha256_bytes(canonical_json_bytes(source))
    identity = {
        "schema_version": "capture-intent/v1",
        "source_occurrence_id": record["source_occurrence_id"],
        "source_event_id": record["source_event_id"],
        "occurred_at": record["occurred_at"],
        "checkpoint_reason": record["checkpoint_reason"],
        "chunk_index": record["chunk_index"],
        "chunk_sha256": chunk_sha256,
    }
    actual = (
        record["chunk_sha256"],
        record["complete_input_sha256"],
        record["intent_id"],
    )
    expected = (
        chunk_sha256,
        complete_sha256,
        sha256_bytes(canonical_json_bytes(identity)),
    )
    if actual != expected:
        raise RuntimeError("capture intent identity is invalid")


def ensure_capture_results_directory(queue: object) -> None:
    from reliable_memory import fsync_directory

    path = Path(queue.results_dir)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    else:
        fsync_directory(path.parent)
    if path.is_symlink() or not path.is_dir():
        raise PermissionError("capture results directory is unsafe")
    path.resolve(strict=True).relative_to(Path(queue.state_root).resolve(strict=True))


def capture_decision_relative_path(intent_id: str) -> str:
    from llm_wiki.queue_validation import capture_decision_relative_path as decision_path

    return decision_path(intent_id, "flush")


def index_capture_decision(
    queue: object,
    coordinator: object,
    lease: object,
    active: object,
    task_fence: object,
    intent_fence: object,
    owner: object,
    encoded: bytes,
) -> object:
    from llm_wiki.formats.canonical import sha256_bytes
    from reliable_memory import publish_runtime_file

    relative = capture_decision_relative_path(active.intent_id)
    publish_runtime_file(
        queue.state_root / relative,
        encoded,
        state_root=queue.state_root,
        create_only=True,
    )
    return queue.publish_semantic_decision(
        coordinator,
        task_id=lease.id,
        intent_id=active.intent_id,
        stage="flush",
        decision_path=relative,
        decision_sha256=sha256_bytes(encoded),
        active_link_digest=active.active_digest,
        task_fence=task_fence,
        intent_fence=intent_fence,
        owner=owner,
    )


def held_by_this_task(
    queue: object,
    coordinator: object,
    lease: object,
    active: object,
    fences: tuple[object, object, object],
    indexed: object,
    encoded: bytes,
) -> object:
    """Index a decision nobody indexed yet, or seal this redrive to its ancestor's."""
    task_fence, intent_fence, owner = fences
    if indexed is None:
        return index_capture_decision(
            queue, coordinator, lease, active, task_fence, intent_fence, owner, encoded
        )
    if indexed.task_id != active.task_id:
        queue.adopt_semantic_decision(
            indexed,
            task_id=active.task_id,
            active_link_digest=active.active_digest,
            task_fence=task_fence,
            intent_fence=intent_fence,
            owner=owner,
        )
    return indexed


def capture_terminal_bytes(
    active: object, decision: object, disposition: Mapping[str, object]
) -> bytes:
    from llm_wiki.formats.canonical import canonical_json_bytes

    terminal = {
        "schema_version": "capture-terminal/v1",
        "intent_id": active.intent_id,
        "intent_sha256": active.intent_sha256,
        "semantic_decisions": [
            {
                "stage": decision.stage,
                "decision_path": decision.decision_path,
                "decision_sha256": decision.decision_sha256,
            }
        ],
        "processing_binding": {
            "kind": "task",
            "task_id": active.task_id,
            "active_link_digest": active.active_digest,
        },
        "disposition": dict(disposition),
    }
    encoded = canonical_json_bytes(terminal)
    if len(encoded) > MAX_CAPTURE_TERMINAL_BYTES:
        raise RuntimeError("capture terminal exceeds its byte limit")
    return encoded


def publish_capture_terminal(
    queue: object,
    lease: object,
    active: object,
    task_fence: object,
    intent_fence: object,
    owner: object,
    decision: object,
    disposition: Mapping[str, object],
) -> object:
    from llm_wiki.formats.canonical import sha256_bytes
    from reliable_memory import publish_runtime_file

    encoded = capture_terminal_bytes(active, decision, disposition)
    relative = f"run/queue-results/capture-{active.intent_id}.json"
    publish_runtime_file(
        queue.state_root / relative,
        encoded,
        state_root=queue.state_root,
        create_only=True,
    )
    return queue.complete_capture_terminal(
        lease,
        intent_id=active.intent_id,
        terminal_path=relative,
        terminal_sha256=sha256_bytes(encoded),
        active_link_digest=active.active_digest,
        task_fence=task_fence,
        intent_fence=intent_fence,
        owner=owner,
    )


def commit_capture_markdown(
    queue: object,
    coordinator: object,
    lease: object,
    active: object,
    intent_fence: object,
    owner: object,
    decision: object,
    decision_record: Mapping[str, object],
) -> tuple[object, object]:
    from markdown_transaction import append_captured_knowledge

    sealed = queue.active_capture_binding(None, lease.id)
    if sealed.seal_digest is None or sealed.active_digest != active.active_digest:
        raise RuntimeError("capture decision did not seal the active binding")
    coordinator.project_capture_binding(sealed, intent_fence=intent_fence)
    plan = decision_record["operation_plan"][0]
    transaction = append_captured_knowledge(
        coordinator,
        owner,
        plan["operation_id"],
        coordinator.vault / plan["path"],
        plan["block"].encode("utf-8"),
        preconditions=capture_transaction_preconditions(sealed, intent_fence),
        inherited_bindings=queue.sealed_ancestor_bindings(lease.id, active.intent_id, "flush"),
    )
    if transaction.state != "committed":
        raise RuntimeError("capture Markdown transaction did not commit")
    return sealed, transaction


def capture_markdown_disposition(transaction: object, decision: object) -> dict:
    outputs = [
        {"path": operation.path, "sha256": operation.after_hash}
        for operation in transaction.operations
    ]
    return {
        "kind": "markdown_committed",
        "transaction_id": transaction.id,
        "operation_id": transaction.operation_id,
        "decision_sha256": decision.decision_sha256,
        "outputs": outputs,
    }
