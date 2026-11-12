"""The compile's transactional application of a reviewed plan."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from claim_tree_manifest import snapshot_claim_tree, snapshot_claim_tree_with_content  # noqa: E402
from claims import (
    ClaimIndex,
    IndexedClaim,
    NormalizedClaim,
    claim_ledger_fingerprint,
    parse_claim_ledger,
)
from compile_receipts import (
    SourceSnapshot,
    all_present_parts,
    parts_with_digest,
    receipt_predicate,
    require_compile_active,
)
from contradiction_pipeline import (  # noqa: E402
    ContradictionPipeline,
    StaleLifecycleTarget,
    default_secondary_search,
    supersede_claims_in_page,
)
from evidence_resolver import (  # noqa: E402
    MAX_DAILY_PART_BYTES,  # noqa: F401 - re-exported: callers read the writer's bound here
    )
from llm_wiki.compile_receipt_authority import (
    MAX_RECEIPT_BYTES,
)
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes  # noqa: E402
from llm_wiki.formats.receipts import (  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402
    compile_context_source_identity,
    compile_operation_id,
)
from llm_wiki.formats.source import SourceDescriptor  # noqa: E402
from markdown_transaction import (  # noqa: E402
    MarkdownChange,
    MarkdownCoordinator,
    TransactionFailure,
)
from queue_core import active_or_legacy_memory_queue
from rebuild_memory_index import (  # noqa: E402
    MAX_INDEX_BYTES,
    build_index_bytes,
)

if TYPE_CHECKING:
    from operational_ownership import OwnerLease
from llm_wiki import runtime_roots
from llm_wiki.compile_pipeline_base import (
    COMPILE_PUBLICATION_ATTEMPTS,
    LOG_ARCHIVE_DIRECTORY,
    LOG_ROTATE_BYTES,
    MAX_AFTER_IMAGE_BYTES,
    MAX_LOG_BYTES,
)
from llm_wiki.compile_pipeline_batches import read_compile_receipt
from llm_wiki.compile_pipeline_claims import append_log_bytes, render_page, with_claim_ledger
from llm_wiki.compile_pipeline_operations import target_snapshot
from llm_wiki.compile_pipeline_prompts import (
    require_ready_compile_batch,
    validate_semantic_operation,
)
from llm_wiki.compile_pipeline_receipts import (
    bound_evidence,
    companion_receipt_precondition,
    compile_writer_gate,
    context_dispositions,
    context_source_for,
    operation_content,
    preflight_context_receipts,
    receipt_bytes,
    receipt_v4_bytes,
    require_apply_arguments,
    require_current_compile_targets,
    utc_now,
    v4_manifest,
    v4_source_descriptor,
)
from llm_wiki.compile_pipeline_sources import (
    CompileApplyResult,
    CompileBatch,
    CompileInputs,
    TargetSnapshot,
    compile_source_snapshot,
    pending_daily_parts,
)
from vault_log import vault_log_path


def apply_compile_plan(
    inputs: CompileInputs,
    plan: dict[str, object],
    *,
    action_key: str,
    trigger: str,
    coordinator: MarkdownCoordinator,
    batch: CompileBatch | None = None,
    provider_budget: Mapping[str, object] | None = None,
    owner: OwnerLease | None = None,
    completed_at: str | None = None,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> CompileApplyResult:
    """Materialize and publish one validated plan as one Markdown transaction."""
    require_apply_arguments(plan, inputs, action_key, batch, provider_budget)
    completed_at = completed_at or utc_now()
    if batch is not None:
        preflight_context_receipts(
            inputs,
            plan,
            action_key=action_key,
            batch=batch,
            provider_budget=provider_budget,
            completed_at=completed_at,
        )
    def _publication() -> ApplyPlan:
        return ApplyPlan(
            inputs,
            plan,
            action_key=action_key,
            trigger=trigger,
            coordinator=coordinator,
            batch=batch,
            provider_budget=provider_budget,
            completed_at=completed_at,
            deadline=deadline,
            cancelled=cancelled,
        )

    return _published(
        _publication,
        coordinator,
        owner=owner,
        deadline=deadline,
        cancelled=cancelled,
    )


def _published_once(
    publication: ApplyPlan,
    coordinator: MarkdownCoordinator,
    owner: OwnerLease | None,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> CompileApplyResult:
    with compile_writer_gate(coordinator, owner, deadline, cancelled):
        committed = publication.completed()
    if committed is not None:
        return committed
    publication.assess_claims()
    with compile_writer_gate(coordinator, owner, deadline, cancelled):
        coordinator.recover(owner=owner, deadline=deadline, cancelled=cancelled)
        return publication.publish()


def _published(
    publication: Callable[[], ApplyPlan],
    coordinator: MarkdownCoordinator,
    *,
    owner: OwnerLease | None,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> CompileApplyResult:
    """A compile refused because the notes tree moved under it is tried again.

    A compile carries the whole notes tree as one precondition, because its
    contradiction assessment was computed against exactly that tree, and the
    assessment happens before the writer gate is taken — it reads every page
    and calls a model, which is not work to hold a gate for. So any other
    writer touching any note in that window refuses the entire transaction,
    and the pages the compile had already produced are never written. That
    is not hypothetical: a compile on 2026-09-02 lost two notes this way, and
    it never ran again because the dailies it read were already marked
    compiled.

    Refusal is the correct outcome for that attempt — the assessment really
    was stale. What was missing is the next attempt. Each one re-reads the
    tree, re-assesses against it, and takes the next attempt ordinal, which is
    the same lineage the append path has always used. A plan whose receipts
    already committed returns from `_existing_receipts` without writing twice.
    """
    refusal: TransactionFailure | None = None
    for _ in range(COMPILE_PUBLICATION_ATTEMPTS):
        try:
            return _published_once(
                publication(), coordinator, owner, deadline, cancelled
            )
        except TransactionFailure as exc:
            refusal = _retryable_refusal(exc)
    raise refusal


def _retryable_refusal(failure: TransactionFailure) -> TransactionFailure:
    if failure.code != "precondition_failed":
        raise failure
    return failure


class ApplyPlan:
    """One publication of one validated compile plan.

    Everything the transaction will contain is assembled here first; nothing
    reaches disk until `_commit` prepares and applies the single transaction.
    """

    def __init__(
        self,
        inputs: CompileInputs,
        plan: dict[str, object],
        *,
        action_key: str,
        trigger: str,
        coordinator: MarkdownCoordinator,
        batch: CompileBatch | None,
        provider_budget: Mapping[str, object] | None,
        completed_at: str,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        self.inputs = inputs
        self.context_manifest = []
        if batch is not None:
            self.context_manifest = v4_manifest(inputs)
        self.action_key = action_key
        self.trigger = trigger
        self.coordinator = coordinator
        self.batch = batch
        self.provider_budget = provider_budget
        self.completed_at = completed_at
        self.deadline = deadline
        self.cancelled = cancelled
        self.source_digests = sorted({item.sha256 for item in inputs.dailies})
        self.operations = _plan_operations(plan)
        self.claim_index: ClaimIndex | None = None
        self.claim_tree_manifest: dict[str, object] | None = None
        self.claim_ledger_snapshot: dict[str, str] = {}
        self.claim_groups: list[tuple[ContradictionPipeline, tuple[object, ...]]] = []
        self.changes: list[MarkdownChange] = []
        self.preconditions: dict[str, object] = {}
        self.companion_preconditions: dict[str, object] = {}
        self.pending: dict[str, bytes | None] = {}
        self.touched: list[str] = []
        self.receipt_operations: list[dict[str, str]] = []
        self.evidence_bindings: list[dict[str, str]] = []
        self.dispositions: list[dict[str, str]] = []
        self.operation_id = ""
        self.parent_transaction_id: str | None = None

    # -- claim assessment, outside the writer gate ---------------------------

    def assess_claims(self) -> None:
        """Assess every claim before the gate; nothing is committed here."""
        manifest = snapshot_claim_tree(runtime_roots.vault_root())
        require_current_compile_targets(self.inputs, manifest)
        if not _plan_carries_claims(self.operations):
            return
        self.claim_tree_manifest = manifest
        self.claim_index = ClaimIndex(self.coordinator.state_root, vault=runtime_roots.vault_root())
        self.claim_index.rebuild(self._claim_tree_paths,
                                 ledger_snapshot=self.claim_ledger_snapshot)
        candidates: list[IndexedClaim] = []
        for planned in self.operations:
            self._assess_operation(planned, candidates)

    def _claim_tree_paths(self) -> list[Path]:
        manifest = self.claim_tree_manifest or {"entries": []}
        return [runtime_roots.vault_root() / item["path"] for item in manifest["entries"]]

    def _assess_operation(
        self, planned: Mapping[str, object], candidates: list[IndexedClaim]
    ) -> None:
        claims = operation_content(planned).get("claims", [])
        if not claims:
            return
        path = str(planned["path"])
        pipeline = self._pipeline(path)
        assessments = tuple(
            self._assessment(pipeline, record, path, candidates) for record in claims
        )
        self.claim_groups.append((pipeline, assessments))

    def _pipeline(self, source_page: str) -> ContradictionPipeline:
        return ContradictionPipeline(
            claim_index=self.claim_index,
            # No model is asked: outside the benchmark gate a semantic answer
            # cannot change the decision (semantic supersession is disabled), so
            # the calls only spent tokens and sent claim text out (audit B-3).
            evaluators=(),
            vault=runtime_roots.vault_root(),
            coordinator=self.coordinator,
            source_page=source_page,
            secondary_search=lambda query, limit: default_secondary_search(
                runtime_roots.vault_root(), query, limit,
                deadline=None if self.deadline == math.inf else self.deadline,
                cancelled=self.cancelled,
            ),
        )

    def _assessment(
        self,
        pipeline: ContradictionPipeline,
        record: object,
        path: str,
        candidates: list[IndexedClaim],
    ) -> object:
        """Each claim also sees the claims this same batch proposed before it."""
        normalized = NormalizedClaim(record)
        known = tuple(self.claim_index.candidates(normalized)) + tuple(candidates)
        assessment = pipeline.assess(normalized, candidates=known or None, commit=False)
        candidates.append(IndexedClaim(path, normalized, ledger_backed=False))
        return assessment

    # -- publication, inside the writer gate ---------------------------------

    def completed(self) -> CompileApplyResult | None:
        """A verified committed receipt precedes reassessment of its own writes."""
        require_compile_active(self.deadline, self.cancelled)
        self._require_companion_receipts()
        return self._existing_receipts()

    def publish(self) -> CompileApplyResult:
        committed = self.completed()
        if committed is not None:
            return committed
        # A quarantined claim is carried on its page as `quarantined` and its
        # candidate joins this commit; it does not hold back the batch (audit
        # A-13, docs/research/2026-09-25-a-quarantined-claim-does-not-hold-its-day.md).
        return self._publish_changes()

    def _refresh_claim_tree_precondition(self) -> None:
        if self.claim_tree_manifest is None:
            return
        manifest, contents = snapshot_claim_tree_with_content(runtime_roots.vault_root())
        current = {path: claim_ledger_fingerprint(parse_claim_ledger(content))
                   for path, content in contents.items()}
        if current != self.claim_ledger_snapshot:
            raise TransactionFailure("assessed claim ledger snapshot changed",
                                     "precondition_failed", "quarantined")
        require_current_compile_targets(self.inputs, manifest)
        self.claim_tree_manifest = manifest
        self.preconditions["claim_tree_manifest"] = manifest

    def _require_companion_receipts(self) -> None:
        selection = receipt_predicate(self.coordinator, deadline=self.deadline)
        companions = [part for part in self.inputs.dailies if part.already_compiled]
        for part in companions:
            relative, digest = companion_receipt_precondition(part, selection, self.coordinator,
                                                              deadline=self.deadline)
            self.companion_preconditions[relative] = digest

    def _publish_changes(self) -> CompileApplyResult:
        self._build_changes()
        self._bind_operation_id()
        quarantine = self._apply_claim_policy()
        if quarantine is not None:
            return quarantine
        self._refresh_claim_tree_precondition()
        self._append_index_and_log()
        self._append_receipts()
        return self._commit()

    def _existing_receipts(self) -> CompileApplyResult | None:
        """Complete source receipts recover their authoritative committed transaction."""
        result = _committed_receipt_result(self._read_receipts(), self.coordinator, self.deadline)
        if result is not None:
            clear_compile_source_failures(self.inputs, self.coordinator, deadline=self.deadline)
        return result

    def _read_receipts(self) -> list[dict[str, object] | None]:
        if self.batch is None:
            return [
                read_compile_receipt(digest, self.coordinator)
                for digest in self.source_digests
            ]
        selection = receipt_predicate(self.coordinator, deadline=self.deadline)
        return _batch_pending_receipts(self.inputs, selection)

    def _commit_quarantine(self) -> CompileApplyResult:
        """A quarantined batch publishes candidates only, and no pages."""
        changes: list[MarkdownChange] = []
        paths: list[str] = []
        present: list[str] = []
        for pipeline, assessments in self.claim_groups:
            policy_changes, _preconditions, candidate_paths, present_paths = (
                pipeline.plan_candidate_changes(_forced_quarantine(assessments))
            )
            changes.extend(policy_changes)
            paths.extend(candidate_paths)
            present.extend(present_paths)
        if not changes:
            return self._already_quarantined(present)
        self.claim_groups[0][0].ensure_candidate_parent()
        return self._commit_quarantine_changes(changes, paths)

    def _already_quarantined(self, present: list[str]) -> CompileApplyResult:
        """Nothing new to write: this attempt's own commit, or the candidates of an earlier one."""
        if not present:
            raise ValueError("quarantined compile batch produced no candidates")
        operation_id = self._quarantine_operation_id(present)
        if self.coordinator.committed_attempt(operation_id) is None:
            raise CandidatesAlreadyQuarantined(present)
        return self._quarantine_result(operation_id, present)

    def _quarantine_operation_id(self, paths: list[str]) -> str:
        return "compile-quarantine:" + sha256_bytes(
            canonical_json_bytes(
                {
                    "action_key": self.action_key,
                    "source_digests": self.source_digests,
                    "candidate_paths": sorted(paths),
                }
            )
        )

    def _quarantine_result(self, operation_id: str, paths: list[str]) -> CompileApplyResult:
        committed, sequence = _transaction_authority(self.coordinator, operation_id, deadline=self.deadline)
        return CompileApplyResult(
            committed.id,
            operation_id,
            committed.state,
            tuple(sorted(paths)),
            sequence,
            committed.updated_at,
            self.action_key,
        )

    def _commit_quarantine_changes(
        self, changes: list[MarkdownChange], paths: list[str]
    ) -> CompileApplyResult:
        operation_id = self._quarantine_operation_id(paths)
        transaction = self.coordinator.prepare(
            sorted(changes, key=lambda item: item.path),
            operation_id=operation_id,
            content_guard="model_output",
            preconditions={
                **{path: "absent" for path in paths},
                "claim_tree_manifest": snapshot_claim_tree(runtime_roots.vault_root()),
            },
            deadline=self.deadline,
            cancelled=self.cancelled,
        )
        self.coordinator.apply(
            transaction.id, deadline=self.deadline, cancelled=self.cancelled
        )
        return self._quarantine_result(operation_id, paths)

    # -- the pages themselves ------------------------------------------------

    def _build_changes(self) -> None:
        self.preconditions = {
            item.logical_path: item.sha256 for item in self.inputs.targets
        }
        self.preconditions.update(self.companion_preconditions)
        if self.claim_tree_manifest is not None:
            self.preconditions["claim_tree_manifest"] = self.claim_tree_manifest
        for planned in self.operations:
            self._build_operation(planned)

    def _build_operation(self, planned: Mapping[str, object]) -> None:
        semantic, bindings = validate_semantic_operation(
            operation_content(planned), self.inputs
        )
        path = str(planned["path"])
        if path != f"knowledge/notes/{semantic['slug']}.md":
            raise ValueError("compile operation path does not match its slug")
        references = [binding["reference"] for binding in bindings]
        page = self._page_bytes(planned, semantic, references, path)
        if len(page) > MAX_AFTER_IMAGE_BYTES:
            raise ValueError("compiled page exceeds after-image limit")
        self.pending[path] = page
        self.touched.append(path)
        self.receipt_operations.append(
            {"kind": str(planned["kind"]), "path": path, "after_sha256": sha256_bytes(page)}
        )
        self.evidence_bindings.extend(bound_evidence(path, bindings, self.inputs))

    def _page_bytes(
        self,
        planned: Mapping[str, object],
        semantic: Mapping[str, object],
        references: list[str],
        path: str,
    ) -> bytes:
        claims = self._rendered_claims(semantic, path)
        target = target_snapshot(self.inputs, path)
        if planned["kind"] == "replace":
            return self._replaced_page(path, target, semantic, references, claims)
        return self._created_page(path, target, semantic, references, claims)

    def _replaced_page(
        self,
        path: str,
        target: TargetSnapshot | None,
        semantic: Mapping[str, object],
        references: list[str],
        claims: list[dict[str, object]],
    ) -> bytes:
        if target is None:
            raise ValueError("replace target was absent from snapshot")
        update = update_section(semantic, references, self.completed_at)
        original = self._updated_claim_history(path, target.content)
        page = with_claim_ledger(original.rstrip() + update, claims)
        self.changes.append(
            MarkdownChange.replace(path, page, max_before_bytes=MAX_AFTER_IMAGE_BYTES)
        )
        self.preconditions[path] = target.sha256
        return page

    def _updated_claim_history(self, path: str, content: bytes) -> bytes:
        mutations = tuple(mutation for mutation in self._lifecycle_mutations() if mutation.page == path)
        if not mutations:
            return content
        return supersede_claims_in_page(content, mutations, path)[0]

    def _lifecycle_mutations(self) -> tuple[object, ...]:
        return tuple(sorted({
            mutation for _pipeline, assessments in self.claim_groups
            for mutation in _assessment_lifecycle_targets(assessments)
        }))

    def _external_lifecycle_groups(self) -> dict[str, list]:
        updated = {str(item["path"]) for item in self.operations if item["kind"] == "replace"}
        groups: dict[str, list] = {}
        for mutation in self._lifecycle_mutations():
            if mutation.page not in updated:
                groups.setdefault(mutation.page, []).append(mutation)
        return groups

    def _lifecycle_pipelines(self) -> dict[str, object]:
        # The last contributing source is the source that completes the page's
        # supersession, just as with successive individually committed updates.
        pipelines: dict[str, object] = {}
        for pipeline, assessments in self.claim_groups:
            for mutation in _assessment_lifecycle_targets(assessments):
                pipelines[mutation.page] = pipeline
        return pipelines

    def _created_page(
        self,
        path: str,
        target: TargetSnapshot | None,
        semantic: Mapping[str, object],
        references: list[str],
        claims: list[dict[str, object]],
    ) -> bytes:
        if target is not None:
            raise ValueError("create target existed in snapshot")
        rendered = render_page(semantic, self.completed_at, references)
        page = with_claim_ledger(rendered, claims)
        self.changes.append(
            MarkdownChange.create(path, page, max_before_bytes=MAX_AFTER_IMAGE_BYTES)
        )
        self.preconditions[path] = "absent"
        return page

    def _rendered_claims(
        self, semantic: Mapping[str, object], path: str
    ) -> list[dict[str, object]]:
        """Quarantine is recorded on the claim, not on the page carrying it."""
        quarantined = {
            str(item.claim.record["id"])
            for item in self._assessments_for(path)
            if item.recommendation == "quarantine"
        }
        return [
            {**record, "lifecycle": _claim_lifecycle(record, quarantined)}
            for record in semantic.get("claims", [])
        ]

    def _assessments_for(self, path: str) -> tuple[object, ...]:
        return next(
            (
                group
                for pipeline, group in self.claim_groups
                if pipeline.source_page == path
            ),
            (),
        )

    # -- identity, claim policy, index, log and receipts ---------------------

    def _bind_operation_id(self) -> None:
        """The v3 identity binds the dispositions, so it waits for the bindings."""
        if self.batch is None:
            self.operation_id = "compile:" + sha256_bytes(
                canonical_json_bytes(
                    {
                        "action_key": self.action_key,
                        "source_digests": self.source_digests,
                    }
                )
            )
            return
        manifest = self.context_manifest
        self.dispositions = context_dispositions(manifest, self.evidence_bindings)
        self.operation_id = compile_operation_id(
            self.action_key, sha256_bytes(canonical_json_bytes(manifest)), self.dispositions
        )

    def _apply_claim_policy(self) -> CompileApplyResult | None:
        """Compose every ledger once while preserving each candidate's source."""
        try:
            candidate_needed = self._stage_claim_candidates()
            self._stage_lifecycle_changes()
        except StaleLifecycleTarget:
            return self._commit_quarantine()
        if candidate_needed:
            self.claim_groups[0][0].ensure_candidate_parent()
        return None

    def _stage_claim_candidates(self) -> bool:
        candidate_needed = False
        for pipeline, assessments in self.claim_groups:
            candidates = tuple(_without_lifecycle(item) for item in assessments)
            changes, preconditions, candidate_paths = pipeline.plan_changes(candidates)
            candidate_needed = candidate_needed or bool(candidate_paths)
            self._add_policy_changes(changes, preconditions)
        return candidate_needed

    def _stage_lifecycle_changes(self) -> None:
        pipelines = self._lifecycle_pipelines()
        for path, mutations in self._external_lifecycle_groups().items():
            changes, preconditions = pipelines[path]._lifecycle_changes(mutations)  # noqa: SLF001
            self._add_policy_changes(changes, preconditions)

    def _add_policy_changes(
        self, changes: Sequence[MarkdownChange], preconditions: Mapping[str, object]
    ) -> None:
        known = {item.path for item in self.changes}
        for change in changes:
            _require_unclaimed_path(known, change.path)
            self.changes.append(change)
            self.preconditions[change.path] = preconditions.get(change.path, "absent")
            self._remember_pending(change)
            self.touched.append(change.path)

    def _remember_pending(self, change: MarkdownChange) -> None:
        """Only note pages feed the index rebuild."""
        if not change.path.startswith("knowledge/notes/"):
            return
        if change.content is None:
            return
        self.pending[change.path] = change.content

    def _append_index_and_log(self) -> None:
        # Both derived targets are materialized under the existing writer gate.
        # Source/target-note snapshots still govern the semantic changes.
        sources = self._publication_sources()
        index_bytes = build_index_bytes(runtime_roots.vault_root(), self.pending)
        self._append_vault_file(
            "knowledge/index.md", index_bytes, sources, MAX_INDEX_BYTES
        )
        log_relative = vault_log_path(runtime_roots.vault_root()).relative_to(runtime_roots.vault_root()).as_posix()
        log_source = sources.get(log_relative)
        log_before = _log_before(log_source)
        log_bytes = append_log_bytes(log_before, self._log_entry())
        if len(log_bytes) > LOG_ROTATE_BYTES:
            log_bytes = self._rotated_log(log_before)
        self._append_vault_file(log_relative, log_bytes, sources, MAX_LOG_BYTES)

    def _rotated_log(self, log_before: bytes) -> bytes:
        """Archive the whole log in this transaction and start a fresh one naming it.

        The compile rewrote the log whole and refused past 4 MiB, so every compile
        failed from then on (docs/research/2026-09-25-the-vault-log-rotates-before-its-cap.md).
        """
        archive = f"{LOG_ARCHIVE_DIRECTORY}/log.local.{self.completed_at[:10]}.md"
        self.coordinator.ensure_target_parent(archive)
        self.preconditions[archive] = "absent"
        self.changes.append(MarkdownChange.create(archive, log_before, max_before_bytes=MAX_LOG_BYTES))
        fresh = (
            f"# Session Memory Log\n\n- {self.completed_at[:10]} — Rotated: earlier entries are "
            f"in `{archive}`.\n"
        ).encode()
        return append_log_bytes(fresh, self._log_entry())

    def _publication_sources(self) -> dict[str, SourceSnapshot]:
        """Current derived-file before-images; prepare still checks their hashes."""
        snapshots = (
            compile_source_snapshot(path, label="compile publication target")
            for path in (runtime_roots.index_file(), vault_log_path(runtime_roots.vault_root())) if path.exists()
        )
        return {item.logical_path: item for item in snapshots}

    def _append_vault_file(
        self,
        path: str,
        content: bytes,
        sources: Mapping[str, object],
        maximum: int,
    ) -> None:
        source = sources.get(path)
        if source is None:
            self.preconditions[path] = "absent"
            self.changes.append(
                MarkdownChange.create(path, content, max_before_bytes=maximum)
            )
            return
        self.preconditions[path] = source.sha256
        self.changes.append(
            MarkdownChange.replace(path, content, max_before_bytes=maximum)
        )

    def _log_entry(self) -> str:
        touched = _touched_phrase(self.touched)
        return (
            f"- {self.completed_at[:10]} — {_trigger_word(self.trigger)} "
            f"compile completed for snapshot {', '.join(self.source_digests)}. "
            f"Touched: {touched}."
        )

    def _append_receipts(self) -> None:
        for source in self._receipt_descriptors():
            self._append_receipt(source)

    def _receipt_descriptors(self) -> tuple[SourceDescriptor, ...]:
        if self.batch is not None:
            return pending_daily_parts(self.inputs)
        return tuple(
            SourceDescriptor(item.logical_path, len(item.content), item.sha256)
            for item in self.inputs.dailies
        )

    def _append_receipt(self, source: SourceDescriptor) -> None:
        relative = self._receipt_relative(source)
        self.coordinator.ensure_target_parent(relative)
        receipt = self._receipt_body(source)
        if len(receipt) > MAX_RECEIPT_BYTES:
            raise ValueError("compile receipt exceeds after-image limit")
        self.changes.append(
            MarkdownChange.create(
                relative, receipt, max_before_bytes=MAX_RECEIPT_BYTES
            )
        )
        self.preconditions[relative] = "absent"

    def _receipt_relative(self, source: SourceDescriptor) -> str:
        if self.batch is None:
            return f"knowledge/daily/receipts/{source.sha256}.md"
        identity = compile_context_source_identity(context_source_for(source, self.context_manifest))
        return f"knowledge/daily/receipts/v4-{identity}.md"

    def _receipt_body(self, source: SourceDescriptor) -> bytes:
        if self.batch is None:
            return receipt_bytes(
                source.sha256,
                self.source_digests,
                self.action_key,
                self.operation_id,
                self.receipt_operations,
                self.evidence_bindings,
                self.completed_at,
            )
        return receipt_v4_bytes(
            context_source_for(source, self.context_manifest),
            manifest=self.context_manifest,
            packing=self.batch.packing,
            provider_budget=self.provider_budget,
            action_key=self.action_key,
            operations=self.receipt_operations,
            evidence=self.evidence_bindings,
        )

    def _commit(self) -> CompileApplyResult:
        # A refused attempt keeps its id and its evidence; this one takes the
        # next ordinal so the same dailies stay compilable. The receipts keep
        # naming the derived identity, because their own readers recompute it
        # from the record; the committed attempt is found through that identity.
        attempt_id, self.parent_transaction_id = (
            self.coordinator.attempt_operation_id(
                self.operation_id, deadline=self.deadline, cancelled=self.cancelled
            )
        )
        transaction = self.coordinator.prepare(
            self.changes,
            operation_id=attempt_id,
            content_guard="model_output",
            preconditions=self.preconditions,
            deadline=self.deadline,
            cancelled=self.cancelled,
            _parent_transaction_id=self.parent_transaction_id,
        )
        self.coordinator.apply(
            transaction.id, deadline=self.deadline, cancelled=self.cancelled
        )
        committed, sequence = _transaction_authority(
            self.coordinator, self.operation_id, deadline=self.deadline
        )
        _rebuild_claim_index(self.claim_index)
        clear_compile_source_failures(self.inputs, self.coordinator, deadline=self.deadline)
        return CompileApplyResult(
            committed.id,
            self.operation_id,
            committed.state,
            tuple(self.touched),
            sequence,
            committed.updated_at,
            self.action_key,
        )


def _plan_operations(plan: Mapping[str, object]) -> list[dict[str, object]]:
    operations = plan.get("operations")
    assert isinstance(operations, list)
    return operations


def _plan_carries_claims(operations: Sequence[object]) -> bool:
    return any(_operation_claims(item) for item in operations)


def _operation_claims(planned: object) -> list[object]:
    if not isinstance(planned, dict):
        return []
    claims = operation_content(planned).get("claims")
    if not isinstance(claims, list):
        return []
    return claims


def _batch_pending_receipts(inputs, selection):
    return [selection.receipt(part) for part in inputs.dailies if not part.already_compiled]


def _committed_receipt_result(receipts, coordinator, deadline):
    if not receipts or any(item is None for item in receipts):
        return None
    operation_id, action_key = _receipt_authority(receipts)
    transaction, sequence = _transaction_authority(coordinator, operation_id, deadline=deadline)
    return CompileApplyResult(transaction.id, operation_id, 'committed', (), sequence,
                              transaction.updated_at, action_key)


def compile_batch_completion(batch, coordinator, *, deadline=math.inf, cancelled=None):
    """Verify source/companion receipts; caller owns the Markdown writer gate."""
    require_ready_compile_batch(batch)
    require_compile_active(deadline, cancelled)
    selection = receipt_predicate(coordinator, deadline=deadline)
    companions = (part for part in batch.inputs.dailies if part.already_compiled)
    for part in companions:
        companion_receipt_precondition(part, selection, coordinator, deadline=deadline)
    result = _committed_receipt_result(_batch_pending_receipts(batch.inputs, selection), coordinator, deadline)
    require_compile_active(deadline, cancelled)
    return result


def _receipt_authority(receipts: Sequence[Mapping[str, object]]) -> tuple[str, str]:
    ids = {str(item["operation_id"]) for item in receipts}
    keys = {str(item["action_key"]) for item in receipts}
    if len(ids) != 1 or len(keys) != 1:
        raise ValueError("compile receipts disagree about transaction authority")
    return ids.pop(), keys.pop()


class CandidatesAlreadyQuarantined(Exception):
    """Every candidate of a quarantined batch already awaits review; nothing new to write."""

    def __init__(self, paths: Sequence[str]) -> None:
        super().__init__(f"{len(paths)} candidate(s) already await review")
        self.paths = tuple(paths)


def _forced_quarantine(assessments: Sequence[object]) -> tuple[object, ...]:
    return tuple(
        replace(
            assessment,
            recommendation="quarantine",
            lifecycle_mutations=(),
            candidate_path=None,
        )
        for assessment in assessments
    )


def update_section(
    semantic: Mapping[str, object], references: Sequence[str], completed_at: str
) -> bytes:
    return (
        f"\n\n## Update ({completed_at[:10]})\n{semantic['body_markdown']}\n\n"
        "## Evidence\n"
        + "\n".join(
            f"- `{reference}` — {item.get('claim', '')}"
            for item, reference in zip(semantic["evidence"], references)
        )
        + "\n"
    ).encode("utf-8")


def _claim_lifecycle(record: Mapping[str, object], quarantined: set[str]) -> object:
    if str(record["id"]) in quarantined:
        return "quarantined"
    return record["lifecycle"]


def _assessment_lifecycle_targets(assessments: Sequence[object]) -> tuple[object, ...]:
    return tuple(mutation for item in assessments for mutation in item.lifecycle_mutations)


def _without_lifecycle(assessment: object) -> object:
    return replace(assessment, lifecycle_mutations=())


def _require_unclaimed_path(known: set[str], path: str) -> None:
    if path in known:
        raise ValueError("compile claim lifecycle overlaps a compile operation target")
    known.add(path)


def _touched_phrase(touched: Sequence[str]) -> str:
    """Name the pages this repository publishes and count the rest.

    The line lands in the vault log (`vault_log.LOG_RELATIVE`), private since
    2026-09-14 but still filtered: it may be pasted somewhere public. Where the vault is
    also the public source, a private page's slug is itself personal content,
    so it is counted instead of named. A vault that publishes everything reads
    exactly as before.
    """
    from rebuild_memory_index import published_paths

    named, hidden = published_paths(runtime_roots.vault_root(), touched)
    if not named and not hidden:
        return "none"
    parts = [*named]
    if hidden:
        parts.append(f"{hidden} unpublished page(s)")
    return ", ".join(parts)


def _log_before(log_source: object) -> bytes:
    if log_source is None:
        return b"# Session Memory Log\n"
    return log_source.content


def _trigger_word(trigger: str) -> str:
    if trigger == "auto":
        return "Automated"
    return "Manual"


def _rebuild_claim_index(claim_index: ClaimIndex | None) -> None:
    """A failed rebuild must not leave a half-written derived index on disk."""
    if claim_index is None:
        return
    try:
        claim_index.rebuild()
    except Exception:  # noqa: BLE001 - the claim index is derived and disposable
        _discard_claim_index(claim_index)


def _discard_claim_index(claim_index: ClaimIndex) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            Path(f"{claim_index.path}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass


def _transaction_authority(
    coordinator: MarkdownCoordinator, operation_id: str, *, deadline: float | None = None
) -> tuple[object, int]:
    transaction = coordinator.committed_attempt(operation_id, deadline=deadline)
    if transaction is None:
        raise ValueError("compile transaction is not committed")
    with coordinator._authority_read_connection(deadline) as database:
        row = database.execute(
            'SELECT rowid AS commit_sequence FROM "transaction" WHERE id = ?',
            (transaction.id,),
        ).fetchone()
    if row is None:
        raise ValueError("compile transaction authority disappeared")
    return transaction, int(row["commit_sequence"])


def existing_receipt_selection(selection, coordinator, deadline):
    if selection is not None:
        return selection
    return receipt_predicate(coordinator, deadline=deadline)


def clear_compile_source_failures(inputs: CompileInputs, coordinator: MarkdownCoordinator, *, deadline: float = math.inf) -> None:
    """Clear only when this verified commit or prior receipts cover every occurrence."""
    require_compile_active(deadline, None)
    queue = active_or_legacy_memory_queue(runtime_roots.vault_root(), coordinator.state_root, deadline=deadline)
    compiled = existing_receipt_selection(None, coordinator, deadline)
    failures = set(queue.source_failure_keys(deadline=deadline))
    for source in inputs.dailies:
        require_compile_active(deadline, None)
        _clear_recorded_source_failure(queue, source, inputs, compiled, failures, deadline)


def _clear_recorded_source_failure(queue, source, inputs, compiled, failures, deadline):
    if (source.logical_path, source.sha256) not in failures:
        return
    if _failure_digest_resolved(source, inputs, compiled):
        queue.clear_source_failure(source.logical_path, source.sha256, deadline=deadline)


def _failure_digest_resolved(source, inputs, compiled):
    committed = _committed_part_descriptors(inputs, source.logical_path)
    resolved = partial(_part_resolved_by_commit, committed=committed, compiled=compiled)
    with compiled._source_parts(source.logical_path) as current:
        return all_present_parts(parts_with_digest(current, source.sha256), resolved)


def _committed_part_descriptors(inputs, logical_path):
    return [v4_source_descriptor(part) for part in inputs.dailies if part.logical_path == logical_path]


def _part_resolved_by_commit(part, committed, compiled):
    """The caller already verified this commit; prior commits still require their receipt."""
    return any(compiled._context_matches(source, part) for source in committed) or compiled.matches(part)
