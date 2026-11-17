"""A capture decision has one size bound, for its writer and every reader (audit 2026-09-27 B-3).

docs/research/2026-09-27-one-file-one-bound.md
"""
from __future__ import annotations

import ast
from pathlib import Path

import operational_ownership
import queue_core
import reliability_v3_validation
from reliable_memory import canonical_json_bytes, publish_runtime_file, sha256_bytes

from tests.queue_engine_sources import queue_engine_tree
from tests.queue_v3_capture_links_helpers import _capture_binding, _coordinator, _queue

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_a_decision_larger_than_the_old_reader_bound_is_indexed(tmp_path: Path) -> None:
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    intent_id = "9" * 64
    binding = _capture_binding(queue, coordinator, registry, intent_id=intent_id, intent_sha256="c" * 64)
    queue.claim_capture("worker")
    decision = canonical_json_bytes({"schema_version": "semantic-decision/v1", "stage": "flush", "body": "x" * 90_000})
    key = sha256_bytes(canonical_json_bytes({"intent_id": intent_id, "stage": "flush"}))
    decision_path = f"run/queue-results/capture-decision-{key}.json"
    (tmp_path / decision_path).parent.mkdir(parents=True, exist_ok=True)
    publish_runtime_file(tmp_path / decision_path, decision, state_root=tmp_path, create_only=True)
    owner = registry.acquire("queue-worker", scope="worker:flush")
    with queue.queue_owner(role="queue-worker", scope="worker:flush", parent=owner), queue_core.capture_task_fences(
        queue, coordinator, binding.task_id, intent_id=intent_id, mode="worker", owner=owner
    ) as (task_fence, intent_fence):
        indexed = queue.publish_semantic_decision(
            coordinator, task_id=binding.task_id, intent_id=intent_id, stage="flush",
            decision_path=decision_path, decision_sha256=sha256_bytes(decision),
            active_link_digest=binding.active_digest, task_fence=task_fence, intent_fence=intent_fence, owner=owner,
        )
    registry.release(owner)

    assert indexed is not None


def _bound_of(call: ast.Call) -> list[str]:
    return [ast.unparse(keyword.value) for keyword in call.keywords if keyword.arg == "max_bytes"]


def _runtime_reads(function: ast.AST) -> list[ast.Call]:
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    return [call for call in calls if getattr(call.func, "id", "") == "read_runtime_bytes"]


def _is_decision_function(node: ast.AST) -> bool:
    return isinstance(node, ast.FunctionDef) and "decision" in node.name


def _decision_functions(tree: ast.AST) -> list[ast.AST]:
    return [node for node in ast.walk(tree) if _is_decision_function(node)]


def _decision_read_bounds(tree: ast.AST) -> list[str]:
    """The `max_bytes` of every runtime read inside a function about a decision."""
    reads = [call for function in _decision_functions(tree) for call in _runtime_reads(function)]
    return [bound for call in reads for bound in _bound_of(call)]


def test_every_decision_reader_uses_the_one_bound() -> None:
    bounds = {
        "queue engine": _decision_read_bounds(queue_engine_tree()),
        "flush_memory.py": _decision_read_bounds(ast.parse((SCRIPTS / "flush_memory.py").read_text(encoding="utf-8"))),
    }

    assert {name: set(found) for name, found in bounds.items()} == {
        "queue engine": {"MAX_CAPTURE_DECISION_BYTES"},
        "flush_memory.py": {"MAX_CAPTURE_DECISION_BYTES"},
    }


def test_capture_policy_aliases_and_repair_reader_share_the_leaf_values():
    import memory_state
    import reliable_memory
    from llm_wiki import reliability_defaults

    assert memory_state.MAX_CAPTURE_INTENT_BYTES is reliability_defaults.MAX_CAPTURE_INTENT_BYTES
    assert reliable_memory.MAX_CAPTURE_DECISION_BYTES is reliability_defaults.MAX_CAPTURE_DECISION_BYTES
    assert reliability_v3_validation.MAX_CAPTURE_INTENT_BYTES is reliability_defaults.MAX_CAPTURE_INTENT_BYTES
