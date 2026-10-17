"""Verify destinations before handler-2 completion or cleanup can be accepted."""
from __future__ import annotations

from pathlib import Path

import breadcrumb_decision
import breadcrumb_evidence
import breadcrumb_storage
from bounded_io import read_stable_bytes
from markdown_transaction import MAX_KNOWLEDGE_TARGET_BYTES, committed_append_content_matches
from reliable_memory import MAX_CAPTURE_DECISION_BYTES, read_runtime_bytes, sha256_bytes


def _equal(actual, expected) -> None:
    if actual != expected:
        raise ValueError("breadcrumb terminal proof conflicts with durable evidence")


def _decision(queue, binding, terminal, bundle):
    indexed = queue.indexed_capture_decision(
        task_id=binding.task_id, intent_id=binding.intent_id, stage="flush",
        active_link_digest=binding.active_digest,
    )
    if indexed is None:
        raise ValueError("breadcrumb terminal has no indexed receipt")
    descriptor = {
        "stage": "flush", "decision_path": indexed.decision_path,
        "decision_sha256": indexed.decision_sha256,
    }
    _equal(terminal["semantic_decisions"], [descriptor])
    data = read_runtime_bytes(
        queue.state_root / indexed.decision_path, queue.state_root,
        max_bytes=MAX_CAPTURE_DECISION_BYTES, owner_only=True,
    )
    _equal(sha256_bytes(data), indexed.decision_sha256)
    return indexed, breadcrumb_decision.read_decision(data, bundle)


def _source(vault: Path, decision: dict, bundle) -> None:
    relative = decision["source"]["path"]
    document = breadcrumb_evidence._read_source_document(vault, relative)
    _equal(sha256_bytes(document), decision["source"]["sha256"])
    _equal(breadcrumb_evidence.read_permanent_source(vault, relative), bundle.content)


def _binding_context(queue, binding, transaction) -> None:
    current = {
        "intent_id": binding.intent_id, "task_id": binding.task_id,
        "active_link_digest": binding.active_digest, "seal_digest": binding.seal_digest,
    }
    inherited = queue.sealed_ancestor_bindings(binding.task_id, binding.intent_id, "flush")
    if binding.seal_digest is None or transaction.preconditions.get("capture_binding") not in (current, *inherited):
        raise ValueError("breadcrumb journal transaction has a foreign binding")
    fence = transaction.preconditions.get("intent_fence", {})
    _equal((fence.get("intent_id"), fence.get("mode")), (binding.intent_id, "worker"))


def _operation_id_matches(actual: str, planned: str) -> bool:
    if actual == planned:
        return True
    if not actual.startswith(planned + ":cas:"):
        return False
    suffix = actual.removeprefix(planned + ":cas:")
    return _canonical_attempt(suffix)


def _canonical_attempt(suffix: str) -> bool:
    return suffix.isascii() and suffix.isdecimal() and str(int(suffix)) == suffix and int(suffix) > 0


def _transaction(queue, disposition, indexed):
    transaction = queue._intent_coordinator()._record(disposition["transaction_id"])
    _equal(disposition, {
        "kind": "markdown_committed", "transaction_id": transaction.id,
        "operation_id": transaction.operation_id, "decision_sha256": indexed.decision_sha256,
        "outputs": [{"path": op.path, "sha256": op.after_hash} for op in transaction.operations],
    })
    return transaction


def _journal_content(queue, relative: str) -> bytes:
    target = queue.vault / relative
    target.resolve().relative_to(queue.vault.resolve(strict=True))
    try:
        return read_stable_bytes(target, MAX_KNOWLEDGE_TARGET_BYTES, label="breadcrumb journal proof")
    except FileNotFoundError:
        return _archived_journal(queue, target.stem)


def _archived_journal(queue, day: str) -> bytes:
    from evidence_resolver import EvidenceResolver

    resolver = EvidenceResolver(queue.vault, state_root=queue.state_root)
    bags = resolver._validated_bags(resolver.archive_root / day[:7])
    matches = [bag for bag in bags if bag.manifest["logical_daily_id"] == day]
    _equal(len(matches), 1)
    return matches[0].payload


def _journal(queue, plan: dict, transaction) -> None:
    content = _journal_content(queue, plan["path"])
    _equal(committed_append_content_matches(
        transaction, plan["path"], plan["block"].encode("utf-8"), content,
    ), True)


def require_journal_transaction(queue, binding, plan: dict, transaction) -> None:
    """The same proof is required for first completion and delayed replay."""
    _equal(transaction.state, "committed")
    _equal(_operation_id_matches(transaction.operation_id, plan["operation_id"]), True)
    _binding_context(queue, binding, transaction)
    _journal(queue, plan, transaction)


def require_terminal_proof(queue, binding, terminal: dict) -> None:
    """No producer dispatch depends on this until all cutover gates pass."""
    vault = queue.vault
    if vault is None:
        raise ValueError("breadcrumb terminal proof requires the actual vault root")
    _equal(terminal["disposition"]["kind"], "markdown_committed")
    bundle = breadcrumb_storage.load_bound_bundle(queue.state_root, binding.intent_id, binding.intent_sha256)
    indexed, decision = _decision(queue, binding, terminal, bundle)
    _source(vault, decision, bundle)
    plan = decision["operation_plan"][0]
    transaction = _transaction(queue, terminal["disposition"], indexed)
    require_journal_transaction(queue, binding, plan, transaction)
