"""The compile's v4 receipts, materialized operations and writer gate."""

from __future__ import annotations

import json
import math
import re
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bounded_io import (  # noqa: E402
    immutable_source_digest,
    read_stable_bytes,
)
from compile_receipts import (
    DailySnapshot,
    physical_source,
    require_compile_active,
    snapshot_absolute_bounds,
)
from evidence_resolver import (  # noqa: E402
    MAX_DAILY_PART_BYTES,  # noqa: F401 - re-exported: callers read the writer's bound here
    EvidenceRef,
)
from llm_wiki.compile_receipt_authority import (
    MAX_RECEIPT_BYTES,
    read_compile_receipt_version,
    require_transaction_authority,
)
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes  # noqa: E402
from llm_wiki.formats.receipts import (  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402
    COMPILE_RECEIPT_SCHEMA,
    COMPILE_RECEIPT_V4_SCHEMA,
    compile_context_source_identity,
    compile_operation_id,
    parse_compile_receipt_v4,
    require_v4_descriptor,
)
from llm_wiki.formats.schema import (
    validate_schema,  # noqa: E402
)
from llm_wiki.formats.source import SourceDescriptor  # noqa: E402
from llm_wiki.markdown_writer_gate import writer_wait_window  # noqa: E402
from markdown_transaction import (  # noqa: E402
    TransactionFailure,
    WriterAdmissionTimeout,
)

if TYPE_CHECKING:
    pass
from llm_wiki import runtime_roots
from llm_wiki.compile_pipeline_claims import reference_parts, render_page, with_claim_ledger
from llm_wiki.compile_pipeline_prompts import (
    require_ready_compile_batch,
    validate_compile_plan,
    validate_semantic_operation,
)
from llm_wiki.compile_pipeline_sources import (
    CompileBatch,
    CompileInputs,
    CompilePackingIdentity,
    deduplicated_sources,
    pending_daily_parts,
    source_descriptor,
)


def receipt_bytes(
    source_digest: str,
    input_digests: list[str],
    action_key: str,
    operation_id: str,
    operations: list[dict[str, str]],
    evidence: list[dict[str, str]],
    completed_at: str,
) -> bytes:
    record = {
        "schema_version": "compile-receipt/v2",
        "source_digest": source_digest,
        "input_digests": input_digests,
        "action_key": action_key,
        "state": "completed",
        "completed_at": completed_at,
        "operation_id": operation_id,
        "operations": operations,
        "evidence": sorted(
            (
                item for item in evidence if item["source_digest"] == source_digest
            ),
            key=lambda item: (
                item["operation_path"], item["source_path"], item["quote_sha256"]
            ),
        ),
    }
    validate_schema(record, COMPILE_RECEIPT_SCHEMA)
    canonical = canonical_json_bytes(record).decode("utf-8")
    return (
        "---\n"
        "type: compile-receipt\n"
        f"source_digest: {source_digest}\n"
        f"action_key: {action_key}\n"
        "status: completed\n"
        f"timestamp: {completed_at}\n"
        "confidence: high\n"
        "source_authority: ai-derived\n"
        "---\n\n"
        "# Compile Receipt\n\n"
        "One-sentence summary: This immutable receipt proves completion of a snapshot compile.\n\n"
        "## Record\n```json\n"
        f"{canonical}\n"
        "```\n"
    ).encode()


def v4_source_descriptor(part: DailySnapshot) -> dict[str, object]:
    physical = physical_source(part)
    start, end = snapshot_absolute_bounds(part)
    _require_snapshot_source_hashes(part, physical)
    if physical.content[start:end] != part.content:
        raise ValueError("compile source context slice disagrees")
    descriptor = source_descriptor(part).receipt_descriptor()
    descriptor.update({
        "original_sha256": physical.sha256,
        "original_byte_size": len(physical.content),
        "byte_start": start,
        "byte_end": end,
    })
    require_v4_descriptor(descriptor)
    return descriptor


def _require_snapshot_source_hashes(part, physical):
    if immutable_source_digest(physical.content) != physical.sha256 or sha256_bytes(part.content) != part.sha256:
        raise ValueError("compile source context digest disagrees")


def v4_manifest(inputs: CompileInputs) -> list[dict[str, object]]:
    deduplicated_sources(inputs.dailies)
    return sorted((v4_source_descriptor(part) for part in inputs.dailies),
                  key=lambda item: (item["logical_path"], item["byte_start"]))


def context_dispositions(
    manifest: Sequence[Mapping[str, object]], evidence: Sequence[Mapping[str, str]]
) -> list[dict[str, str]]:
    compiled_sources = {(item["source_path"], item["source_digest"]) for item in evidence}
    return sorted(({
        "source_identity": compile_context_source_identity(source),
        "disposition": "compiled" if (source["logical_path"], source["sha256"]) in compiled_sources else "no_durable_content",
    } for source in manifest), key=lambda item: item["source_identity"])


def context_source_for(source: SourceDescriptor, manifest) -> dict[str, object]:
    matches = [part for part in manifest
               if _context_descriptor_matches(source, part)]
    if len(matches) != 1:
        raise ValueError("compile receipt work part is ambiguous or absent")
    return matches[0]


def _context_descriptor_matches(source, part):
    if part["logical_path"] != source.logical_path or part["sha256"] != source.sha256:
        return False
    if isinstance(source, DailySnapshot):
        return part["byte_start"] == source.byte_start and part["byte_end"] == source.byte_end
    return True


def receipt_v4_bytes(
    source: Mapping[str, object], *, manifest: Sequence[Mapping[str, object]],
    packing: CompilePackingIdentity, provider_budget: Mapping[str, object],
    action_key: str, operations: list[dict[str, str]], evidence: list[dict[str, str]],
) -> bytes:
    identity = compile_context_source_identity(source)
    dispositions = context_dispositions(manifest, evidence)
    manifest_hash = sha256_bytes(canonical_json_bytes(manifest))
    record = {
        "schema_version": "compile-receipt/v4", "source": dict(source),
        "source_identity": identity, "batch_manifest": list(manifest),
        "batch_manifest_sha256": manifest_hash, "action_key": action_key,
        "operation_id": compile_operation_id(action_key, manifest_hash, dispositions),
        "packing": packing.canonical(), "provider_budget": dict(provider_budget),
        "dispositions": dispositions, "operations": sorted(operations, key=lambda item: item["path"]),
        "evidence": _v4_receipt_evidence(source, identity, evidence),
    }
    validate_schema(record, COMPILE_RECEIPT_V4_SCHEMA)
    return context_receipt_document(record)


def _v4_receipt_evidence(source, identity, evidence):
    return sorted(({"source_identity": identity, **item} for item in evidence
                   if item["source_path"] == source["logical_path"]
                   and item["source_digest"] == source["sha256"]),
                  key=lambda item: (item["operation_path"], item["source_path"], item["quote_sha256"]))


def context_receipt_document(record: Mapping[str, object]) -> bytes:
    return (
        "---\ntype: compile-receipt\nschema_version: compile-receipt/v4\n"
        f"source_identity: {record['source_identity']}\n"
        "status: completed\nconfidence: high\nsource_authority: ai-derived\n---\n\n"
        "# Compile Receipt\n\n"
        "One-sentence summary: This immutable receipt proves completion of a snapshot compile.\n\n"
        "## Record\n```json\n" + canonical_json_bytes(record).decode() + "\n```\n"
    ).encode()


def preflight_context_receipts(
    inputs: CompileInputs,
    plan: dict[str, object],
    *,
    action_key: str,
    batch: CompileBatch,
    provider_budget: Mapping[str, object],
    completed_at: str,
) -> None:
    operations = plan.get("operations")
    assert isinstance(operations, list)
    receipt_operations, evidence_bindings = materialized_operations(
        operations, inputs, completed_at
    )
    manifest = v4_manifest(inputs)
    for source in pending_daily_parts(batch.inputs):
        receipt = receipt_v4_bytes(
            context_source_for(source, manifest),
            manifest=manifest,
            packing=batch.packing,
            provider_budget=provider_budget,
            action_key=action_key,
            operations=receipt_operations,
            evidence=evidence_bindings,
        )
        if len(receipt) > MAX_RECEIPT_BYTES:
            raise ValueError("compile receipt exceeds after-image limit")


def materialized_operations(
    operations: Sequence[object], inputs: CompileInputs, completed_at: str
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Render each planned page to prove its after-image and evidence bindings."""
    receipt_operations: list[dict[str, str]] = []
    evidence_bindings: list[dict[str, str]] = []
    for planned in operations:
        assert isinstance(planned, dict)
        semantic, bindings = validate_semantic_operation(
            operation_content(planned), inputs
        )
        references = [binding["reference"] for binding in bindings]
        page = with_claim_ledger(
            render_page(semantic, completed_at, references),
            semantic.get("claims", []),
        )
        receipt_operations.append(
            {
                "kind": str(planned["kind"]),
                "path": str(planned["path"]),
                "after_sha256": sha256_bytes(page),
            }
        )
        evidence_bindings.extend(bound_evidence(str(planned["path"]), bindings, inputs))
    return receipt_operations, evidence_bindings


def operation_content(planned: Mapping[str, object]) -> dict[str, object]:
    semantic = json.loads(str(planned["content"]))
    if not isinstance(semantic, dict):
        raise ValueError("compile operation content must describe an object")
    return semantic


def bound_evidence(
    operation_path: str, bindings: Sequence[Mapping[str, str]], inputs=None,
) -> list[dict[str, str]]:
    records = _receipt_evidence_records(operation_path, bindings, inputs)
    return list({canonical_json_bytes(item): item for item in records}.values())


def _receipt_evidence_records(operation_path, bindings, inputs):
    if inputs is not None:
        return [item for binding in bindings
                for item in _part_evidence_records(operation_path, binding, inputs)]
    return _legacy_bound_evidence(operation_path, bindings)


def _legacy_bound_evidence(operation_path, bindings):
    return [
        {
            "operation_path": operation_path,
            **{key: value for key, value in binding.items() if key != "reference"},
        }
        for binding in bindings
    ]


def _part_evidence_records(operation_path, binding, inputs):
    reference = EvidenceRef.parse(binding["reference"])
    return [{"operation_path": operation_path, "source_path": part.logical_path,
             "source_digest": part.sha256, "quote_sha256": binding["quote_sha256"]}
            for part in reference_parts(inputs, reference)]


def context_receipt_path(source):
    return runtime_roots.daily_dir() / "receipts" / f"v4-{compile_context_source_identity(source)}.md"


def read_snapshot_receipt(part, coordinator):
    source = v4_source_descriptor(part)
    return read_compile_receipt_version(part.logical_path, part.sha256, coordinator,
                                        path=context_receipt_path(source), vault=runtime_roots.vault_root())


def companion_receipt_precondition(part, selection, coordinator, *, deadline):
    record = selection.receipt(part)
    if record is None:
        raise ValueError("compiled companion receipt is no longer authoritative")
    relative = f"knowledge/daily/receipts/v4-{record['source_identity']}.md"
    path = runtime_roots.vault_root() / relative
    raw = read_stable_bytes(path, MAX_RECEIPT_BYTES, label="compiled companion receipt",
                            deadline=_receipt_io_deadline(deadline))
    parsed = parse_compile_receipt_v4(raw, logical_path=part.logical_path, source_sha256=part.sha256)
    if parsed != record:
        raise ValueError("compiled companion receipt changed during publication")
    require_transaction_authority(record, coordinator, path, runtime_roots.vault_root(), raw, deadline=deadline)
    return relative, sha256_bytes(raw)


def _receipt_io_deadline(deadline):
    if deadline == math.inf:
        return None
    return deadline


@contextmanager
def compile_writer_gate(coordinator, owner, deadline, cancelled):
    """Retry only admission, within the caller's existing whole-call budget."""
    with ExitStack() as stack:
        enter_compile_writer_gate(stack, coordinator, owner, deadline, cancelled)
        require_compile_active(deadline, cancelled)
        yield


def enter_compile_writer_gate(stack, coordinator, owner, deadline, cancelled):
    while True:
        require_compile_active(deadline, cancelled)
        wait = min(writer_wait_window(None), max(0.0, deadline - time.monotonic()))
        try:
            return stack.enter_context(coordinator.writer_gate(owner=owner, wait_seconds=wait))
        except WriterAdmissionTimeout:
            print("compile_memory: writer admission busy; retaining the validated plan and waiting within the compile deadline", file=sys.stderr)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _require_paired_batch(
    batch: object, provider_budget: object
) -> None:
    """A batch without its budget is a plan nobody costed."""
    if (batch is None) != (provider_budget is None):
        raise ValueError("compile batch and provider budget must be supplied together")


def require_apply_arguments(
    plan: dict[str, object],
    inputs: CompileInputs,
    action_key: str,
    batch: CompileBatch | None,
    provider_budget: Mapping[str, object] | None,
) -> None:
    require_ready_compile_batch(batch)
    validate_compile_plan(plan, inputs)
    if not re.fullmatch(r"[0-9a-f]{64}", action_key):
        raise ValueError("action key must be a SHA-256 digest")
    _require_paired_batch(batch, provider_budget)
    if batch is not None and batch.inputs != inputs:
        raise ValueError("compile batch inputs disagree")


def require_current_compile_targets(
    inputs: CompileInputs, manifest: Mapping[str, object]
) -> None:
    """A changed model-input page needs a new plan, not the same assessment.

    The publication retry refreshes its claim tree, but its original target
    bytes remain immutable. Reassessing a plan against changed target bytes
    cannot make those original preconditions true. Refuse before assessment;
    the next compile snapshots and resolves the still-unreceipted source.
    The transaction's own precondition checks remain authoritative.
    """
    current = {entry["path"]: entry["sha256"] for entry in manifest["entries"]}
    for target in inputs.targets:
        if current.get(target.logical_path) != target.sha256:
            raise TransactionFailure(
                f"compile target snapshot changed for {target.logical_path}; "
                "a fresh snapshot and model plan are required",
                "compile_snapshot_changed",
                "quarantined",
            )
