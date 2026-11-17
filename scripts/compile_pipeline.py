"""The compile pipeline: from daily logs to committed notes and receipts."""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_io import (  # noqa: E402
    immutable_source_digest,
)
from compile_cache import (  # noqa: E402
    CompileCache,
)
from compile_receipts import (
    DailySnapshot,
    daily_is_compiled,
    receipt_predicate,
    require_compile_active,
    snapshot_compiled,
)
from context_budget import count_tokens as count_tokens  # noqa: E402
from evidence_resolver import (  # noqa: E402
    MAX_DAILY_PART_BYTES,  # noqa: F401 - re-exported: callers read the writer's bound here
    daily_part_bounds,
)
from llm_client import (  # noqa: E402
    forced_provider,
    probe_candidate,
    provider_candidates,
)
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes  # noqa: E402
from llm_wiki.formats.receipts import (  # noqa: E402
    COMPILE_RECEIPT_SCHEMA as COMPILE_RECEIPT_SCHEMA,
)
from llm_wiki.formats.receipts import (  # noqa: E402
    COMPILE_RECEIPT_V3_SCHEMA as COMPILE_RECEIPT_V3_SCHEMA,
)
from llm_wiki.formats.receipts import (  # noqa: E402
    COMPILE_RECEIPT_V4_SCHEMA as COMPILE_RECEIPT_V4_SCHEMA,
)
from llm_wiki.formats.receipts import (  # noqa: E402
    UnsupportedCompileReceiptVersion as UnsupportedCompileReceiptVersion,
)
from llm_wiki.formats.receipts import (  # noqa: E402
    compile_context_source_identity as compile_context_source_identity,
)
from llm_wiki.formats.receipts import (  # noqa: E402
    compile_operation_id as compile_operation_id,
)
from llm_wiki.formats.receipts import (  # noqa: E402
    compile_source_identity as compile_source_identity,
)
from llm_wiki.formats.receipts import (  # noqa: E402
    corrupt_receipt as corrupt_receipt,
)
from llm_wiki.formats.receipts import (  # noqa: E402
    parse_compile_receipt_v2 as parse_compile_receipt_v2,
)
from llm_wiki.formats.receipts import (  # noqa: E402
    parse_compile_receipt_v3 as parse_compile_receipt_v3,
)
from llm_wiki.formats.receipts import (  # noqa: E402
    parse_compile_receipt_v4 as parse_compile_receipt_v4,
)
from llm_wiki.formats.receipts import (  # noqa: E402
    parse_compile_receipt_version as parse_compile_receipt_version,
)
from llm_wiki.formats.receipts import (  # noqa: E402
    receipt_source_fields as receipt_source_fields,
)
from llm_wiki.formats.receipts import (  # noqa: E402
    receipt_version_reader as receipt_version_reader,
)
from llm_wiki.formats.receipts import (  # noqa: E402
    require_v4_descriptor as require_v4_descriptor,
)
from llm_wiki.formats.schema import validate_rule as validate_rule  # noqa: E402
from llm_wiki.queue_store import MAX_SOURCE_FAILURE_CODE_BYTES
from llm_wiki.utf8_text import utf8_prefix  # noqa: E402
from markdown_transaction import (  # noqa: E402
    MarkdownCoordinator,
    active_or_legacy_coordinator,
)
from memory_state import load_state
from queue_core import active_or_legacy_memory_queue
from rebuild_memory_index import (  # noqa: E402
    IndexCapacityError,
    build_index_bytes,
)

if TYPE_CHECKING:
    from operational_ownership import OwnerLease
from llm_wiki import runtime_roots
from llm_wiki.compile_pipeline_apply import (
    CandidatesAlreadyQuarantined as CandidatesAlreadyQuarantined,
)
from llm_wiki.compile_pipeline_apply import apply_compile_plan as apply_compile_plan
from llm_wiki.compile_pipeline_apply import (
    existing_receipt_selection as existing_receipt_selection,
)
from llm_wiki.compile_pipeline_base import LEGACY_EVIDENCE_SCHEMA as LEGACY_EVIDENCE_SCHEMA
from llm_wiki.compile_pipeline_base import NATIVE_EVIDENCE_SCHEMA as NATIVE_EVIDENCE_SCHEMA
from llm_wiki.compile_pipeline_base import RAW_PLAN_SCHEMA as RAW_PLAN_SCHEMA
from llm_wiki.compile_pipeline_batches import (
    CompileRepairCapacityError as CompileRepairCapacityError,
)
from llm_wiki.compile_pipeline_batches import attach_required_context as attach_required_context
from llm_wiki.compile_pipeline_batches import batch_measure as batch_measure
from llm_wiki.compile_pipeline_batches import compile_batch as compile_batch
from llm_wiki.compile_pipeline_batches import measure_owns_inputs as measure_owns_inputs
from llm_wiki.compile_pipeline_batches import measure_partitions as measure_partitions
from llm_wiki.compile_pipeline_batches import (
    pack_planned_compile_batches as pack_planned_compile_batches,
)
from llm_wiki.compile_pipeline_batches import packing_journal_indexes as packing_journal_indexes
from llm_wiki.compile_pipeline_batches import partition_packable as partition_packable
from llm_wiki.compile_pipeline_batches import planned_candidate as planned_candidate
from llm_wiki.compile_pipeline_batches import refresh_compile_batch as refresh_compile_batch
from llm_wiki.compile_pipeline_batches import repair_context_paths as repair_context_paths
from llm_wiki.compile_pipeline_batches import require_repair_context as require_repair_context
from llm_wiki.compile_pipeline_batches import resolve_compile_plan as resolve_compile_plan
from llm_wiki.compile_pipeline_claims import DROPPED_CLAIMS as DROPPED_CLAIMS
from llm_wiki.compile_pipeline_claims import BatchOutcome as BatchOutcome
from llm_wiki.compile_pipeline_claims import committed_outcome as committed_outcome
from llm_wiki.compile_pipeline_claims import outcome_sentence as outcome_sentence
from llm_wiki.compile_pipeline_operations import with_derived_claims as with_derived_claims
from llm_wiki.compile_pipeline_prompts import critique_prompt as critique_prompt
from llm_wiki.compile_pipeline_prompts import packing_budget as packing_budget
from llm_wiki.compile_pipeline_prompts import (
    require_ready_compile_batch as require_ready_compile_batch,
)
from llm_wiki.compile_pipeline_prompts import required_context_paths as required_context_paths
from llm_wiki.compile_pipeline_prompts import source_layout_resolution as source_layout_resolution
from llm_wiki.compile_pipeline_prompts import (
    validated_critique_feedback as validated_critique_feedback,
)
from llm_wiki.compile_pipeline_selection import mark_finished as mark_finished
from llm_wiki.compile_pipeline_selection import mark_ok_unless_dry as mark_ok_unless_dry
from llm_wiki.compile_pipeline_selection import repair_compile_mirror as repair_compile_mirror
from llm_wiki.compile_pipeline_selection import require_state_mapping as require_state_mapping
from llm_wiki.compile_pipeline_selection import select_dailies as select_dailies
from llm_wiki.compile_pipeline_selection import update_state_under_clock as update_state_under_clock
from llm_wiki.compile_pipeline_selection import whole_daily_digest as whole_daily_digest
from llm_wiki.compile_pipeline_sources import CompileApplyResult as CompileApplyResult
from llm_wiki.compile_pipeline_sources import CompileBatch as CompileBatch
from llm_wiki.compile_pipeline_sources import CompileInputs as CompileInputs
from llm_wiki.compile_pipeline_sources import ResolvedCompilePlan as ResolvedCompilePlan
from llm_wiki.compile_pipeline_sources import native_part_units as native_part_units
from llm_wiki.compile_pipeline_sources import (
    open_optional_daily_source as open_optional_daily_source,
)
from llm_wiki.compile_pipeline_sources import snapshot_compile_inputs as snapshot_compile_inputs


def refused_unit_outcome(day: CompileInputs, *, deadline: float = math.inf, record: bool = True) -> BatchOutcome:
    """A retained atomic unit fails alone, recorded like any failed batch."""
    path = day.dailies[0].logical_path
    refusal = ValueError(f"daily source exceeds compile input budget: {path}")
    return BatchOutcome(record_failed_batch(day, refusal, deadline=deadline, record=record))


# A daily log is `YYYY-MM-DD.md`. The directory also ships a README, and the
# lint and the session-start context already filter on this name; compile did
# not, so that one file entered the candidate list and failed the whole pass
# on `logical_path must name a canonical daily source`.
# The rule itself lives in `memory_state` (`DAILY_LOG_NAME`, `daily_logs`), so
# that no reader of the directory can miss it again.


# One compile call may run as long as the setting `provider.draft_ceiling_seconds`
# (600 s by default; its measurements and reason are in scripts/settings.py). The
# client's 90 s default stays for every other call, so a stuck capture flush is still
# heard about quickly; MEMORY_LLM_TIMEOUT_S, when set, overrides both.


def run(
    args: argparse.Namespace,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
    owner: OwnerLease | None = None,
) -> int:
    require_compile_active(deadline, cancelled)
    DROPPED_CLAIMS.clear()
    state = load_state()
    coordinator = active_or_legacy_coordinator(runtime_roots.vault_root(), runtime_roots.state_root(), deadline=deadline)
    selection = receipt_predicate(coordinator, deadline=deadline)
    dailies = select_dailies(args, state, coordinator=coordinator, deadline=deadline, selection=selection)
    _maintain_compile_source_state(args, coordinator, selection, deadline)
    require_compile_active(deadline, cancelled)
    if not dailies:
        print("compile_memory: no changed daily logs; nothing to do.")
        mark_ok_unless_dry(args)
        return 0

    # Publication capacity is shared by every batch; reject before spending on models.
    build_index_bytes(runtime_roots.vault_root())
    _announce_compile(args, dailies)
    inputs = snapshot_compile_inputs(dailies, compiled=selection)
    try:
        batches, refused = pack_for_run(inputs, deadline)
    except Exception as exc:  # noqa: BLE001 - provider/cache boundary is fail-closed
        require_compile_active(deadline, cancelled)
        failed = BatchOutcome(record_failed_batch(inputs, exc, deadline=deadline, record=not args.dry_run))
        return finish_run(args, [failed])

    outcomes: list[BatchOutcome] = [refused_unit_outcome(day, deadline=deadline, record=not args.dry_run)
                                    for day in refused]
    for batch in batches:
        # A failed batch is recorded against its sources and the run goes on:
        # batches are independent snapshots, and stopping here held every later
        # day behind one bad day (audit 2026-09-27 A-3,
        # docs/research/2026-09-27-one-bad-day-does-not-hold-the-rest.md).
        outcomes.extend(
            run_repairable_batch(
                refresh_compile_batch(batch, deadline=deadline),
                args,
                coordinator=coordinator,
                deadline=deadline,
                cancelled=cancelled,
                owner=owner,
            )
        )
    require_compile_active(deadline, cancelled)
    return finish_run(args, outcomes)


def _maintain_compile_source_state(args, coordinator, selection, deadline):
    if args.dry_run:
        return
    repair_compile_mirror(coordinator, deadline=deadline, selection=selection)
    retire_stale_source_failures(coordinator.state_root, coordinator=coordinator,
                                 deadline=deadline, selection=selection)


@dataclass(frozen=True)
class CompileRunPlanning:
    """One resolved candidate order shared by eager packing and saved preflight.

    This is planned behavior, not a live dependency witness. The controller must
    bind request() to admission and verify dependencies before using saved work.
    """
    model: str | None
    candidates: tuple

    def batches(self, reader, on_refused):
        yield from reader.iter_batches(model=self.model, planning_candidates=self.candidates,
                                       context_pending=True, on_refused=on_refused)

    def request(self):
        identity = {'model': self.model,
                    'candidates': [_run_planning_candidate_identity(item) for item in self.candidates],
                    'context_pending': True}
        return {'schema': 'compile-run-planning/v1',
                'planning_digest': sha256_bytes(canonical_json_bytes(identity))}

    def retained(self):
        """Private archive evidence; endpoint/config/environment contents stay absent."""
        return {'schema': 'compile-run-planning-state/v1', 'model': self.model,
                'candidates': [_retained_planning_candidate(item) for item in self.candidates]}


def _retained_planning_candidate(candidate):
    value = _run_planning_candidate_identity(candidate)
    value['endpoint_digest'] = sha256_bytes(canonical_json_bytes(value.pop('endpoint')))
    basis = candidate._codex_basis
    value['original_descriptor'] = None if basis is None else basis.original_descriptor.canonical()
    return value


def restore_compile_planning(document, *, deadline=float('inf')):
    """Use saved observations and current local configuration; never probe providers."""
    require_compile_active(deadline, None)
    try:
        planning = _restore_run_planning(document)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ValueError('invalid or changed retained compile planning') from error
    require_compile_active(deadline, None)
    return planning


def _restore_run_planning(document):
    current = provider_candidates(forced_provider(), max_tokens=4000)
    candidates = tuple(_restore_planning_candidate(saved, local)
                       for saved, local in zip(document['candidates'], current, strict=True))
    planning = CompileRunPlanning(document['model'], candidates)
    _require_retained_planning_model(planning.model)
    if planning.retained() != document:
        raise ValueError('retained planning evidence changed')
    return planning


def _require_retained_planning_model(model):
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError('invalid retained planning model')


def _restore_planning_candidate(saved, current):
    original = saved['original_descriptor'] or saved['descriptor']
    if current.canonical() != original:
        raise ValueError('local planning configuration changed')
    candidate = replace(current, model=saved['descriptor']['model'],
                        _resolution_failure=saved['resolution_failure'])
    basis = _restore_planning_basis(saved, current)
    candidate = replace(candidate, _codex_basis=basis)
    if _retained_planning_candidate(candidate) != saved:
        raise ValueError('retained planning candidate changed')
    return candidate


def _restore_planning_basis(saved, original):
    from llm_client import CodexExecutable, CodexPlanningBasis

    if saved['basis'] is None:
        return None
    executable = dict(saved['executable'])
    executable['identity'] = tuple(executable['identity'])
    fields = dict(saved['basis'])
    fields['config_files'] = tuple(tuple(item) for item in fields['config_files'])
    fields['mcp_server_names'] = tuple(fields['mcp_server_names'])
    return CodexPlanningBasis(original, CodexExecutable(**executable), **fields)


def _run_planning_candidate_identity(candidate):
    from llm_client import prepared_basis_identity

    return {'descriptor': candidate.canonical(), 'endpoint': candidate._endpoint,
            'resolution_failure': candidate.resolution_failure,
            'basis': prepared_basis_identity(candidate._codex_basis),
            'executable': _run_planning_executable_identity(candidate._codex_basis)}


def _run_planning_executable_identity(basis):
    from llm_client import prepared_executable_identity

    if basis is None:
        return None
    return prepared_executable_identity(basis.executable)


def prepare_compile_planning(*, deadline=float('inf')):
    """Resolve the authoritative candidate chain once, before source planning."""
    require_compile_active(deadline, None)
    candidates = tuple(planned_candidate(item, deadline)
                       for item in provider_candidates(forced_provider(), max_tokens=4000))
    model = next((item.model for item in candidates if probe_candidate(item)), None)
    require_compile_active(deadline, None)
    return CompileRunPlanning(model, candidates)


def pack_for_run(inputs, deadline):
    planning = prepare_compile_planning(deadline=deadline)
    packable, refused = partition_packable(inputs, model=planning.model, planning_candidates=planning.candidates)
    return pack_planned_compile_batches(packable, model=planning.model, planning_candidates=planning.candidates,
                                 context_pending=True), refused


def finish_run(args: argparse.Namespace, outcomes: Sequence[BatchOutcome]) -> int:
    """Finalize only after every batch has stopped using the compile lock."""
    failed = [item for item in outcomes if item.status != 0]
    if failed:
        error = str(load_state().get("last_compile_error", "one or more compile batches failed"))
        _mark_failed_run_unless_dry(args, error, outcomes)
        print(f"compile_memory: {len(failed)} batch(es) failed; the rest: {outcome_sentence(outcomes)}.")
        return 1
    mark_ok_unless_dry(args, outcomes=outcomes)
    print(f"compile_memory: done: {outcome_sentence(outcomes)}.")
    return 0


def _mark_failed_run_unless_dry(args, error, outcomes):
    if args.dry_run:
        return
    mark_finished(args.trigger, 'error', error, outcomes=outcomes)


def _announce_compile(args: argparse.Namespace, dailies: Sequence[Path]) -> None:
    suffix = " (dry-run)" if args.dry_run else ""
    print(f"compile_memory: compiling {len(dailies)} daily log(s){suffix}:")
    for path in dailies:
        print(f"  - {path.relative_to(runtime_roots.vault_root()).as_posix()}")


def record_failed_batch(
    inputs: CompileInputs,
    exc: BaseException,
    *,
    prefix: str = "",
    deadline: float = math.inf,
    record: bool = True,
) -> int:
    """Record a batch failure without finishing or unlocking the active run."""
    error = f"{type(exc).__name__}: {exc}"
    print(f"compile_memory: FAILED — {prefix}{error}", flush=True)
    if not record:
        return 1
    record_compile_source_failures(inputs, runtime_roots.state_root(), error_code=type(exc).__name__, deadline=deadline)
    _record_batch_error(error, deadline=deadline)
    return 1


def _record_batch_error(error: str, *, deadline: float = math.inf) -> None:
    def remember(state: dict) -> None:
        state["last_compile_error"] = error[:500]

    update_state_under_clock(remember, deadline)


def run_batch(
    batch: CompileBatch,
    args: argparse.Namespace,
    *,
    coordinator: MarkdownCoordinator,
    deadline: float,
    cancelled: Callable[[], bool] | None,
    owner: OwnerLease | None,
    critic_feedback: tuple[str, ...] = (),
) -> BatchOutcome:
    """Resolve and apply one batch; a non-zero status marks it failed, not the run over."""
    require_ready_compile_batch(batch)
    try:
        resolved = _resolve_batch_work(batch, coordinator, critic_feedback)
    except CompileRepairCapacityError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider/cache boundary is fail-closed
        require_compile_active(deadline, cancelled)
        return BatchOutcome(record_failed_batch(batch.inputs, exc, deadline=deadline, record=not args.dry_run))

    require_compile_active(deadline, cancelled)
    if args.dry_run:
        print(
            f"compile_memory: dry-run resolved {len(resolved.plan['operations'])} "
            f"operation(s){' from cache' if resolved.cache_hit else ''}; no writes."
        )
        return BatchOutcome(0)
    return _apply_batch(
        batch,
        resolved,
        args,
        coordinator=coordinator,
        deadline=deadline,
        cancelled=cancelled,
        owner=owner,
    )


def _resolve_batch_work(batch, coordinator, feedback):
    options = {'coordinator': coordinator, 'batch': batch}
    if feedback:
        options['critic_feedback'] = feedback
    return resolve_compile_plan(batch.inputs, CompileCache(runtime_roots.state_root()), **options)


def run_repairable_batch(batch, args, *, coordinator, deadline, cancelled, owner,
                          critic_feedback=()):
    options = dict(coordinator=coordinator, deadline=deadline, cancelled=cancelled, owner=owner)
    try:
        return [run_batch(batch, args, critic_feedback=critic_feedback, **options)]
    except CompileRepairCapacityError as error:
        require_compile_active(deadline, cancelled)
        children = repartition_compile_repair(batch, error.feedback)
        if not children:
            return [BatchOutcome(record_failed_batch(batch.inputs, error, deadline=deadline, record=not args.dry_run))]
        print(f'compile_memory: {error}; retrying complete source partitions with every reviewed proposal.',
              flush=True)
        return run_repair_children(children, args, options)


def run_repair_children(children, args, options):
    outcomes = []
    for child, feedback in children:
        require_compile_active(options['deadline'], options['cancelled'])
        outcomes.extend(run_repair_child(child, feedback, args, options))
    return outcomes


def run_repair_child(child, feedback, args, options):
    try:
        refreshed = refresh_compile_batch(child, deadline=options['deadline'], retained_context=True)
        require_repair_context(child, refreshed)
    except Exception as error:  # noqa: BLE001 - a failed child cannot discard its independent sibling
        require_compile_active(options['deadline'], options['cancelled'])
        return [BatchOutcome(record_failed_batch(child.inputs, error, deadline=options['deadline'],
                                                  record=not args.dry_run))]
    return run_repairable_batch(refreshed, args, critic_feedback=feedback, **options)


def repartition_compile_repair(batch, feedback):
    units = native_part_units(batch.inputs.dailies)
    if len(units) < 2:
        return ()
    measure = batch_measure(batch.inputs, batch.planning_model, None)
    attach_required_context(measure, batch.inputs, batch.planning_candidates)
    with source_layout_resolution(batch.inputs):
        children = _first_repair_partition(batch, units, feedback, measure)
        if children is not None:
            return children
    return ()


def _first_repair_partition(batch, units, feedback, measure):
    for cut in _balanced_repair_cuts(units):
        children = _repair_partition_at(batch, units, cut, feedback, measure)
        if children is not None:
            return children
    return None


def _balanced_repair_cuts(units):
    sizes = [sum(len(part.content) for part in unit) for unit in units]
    total = sum(sizes)
    return sorted(range(1, len(units)), key=lambda cut: abs(2 * sum(sizes[:cut]) - total))


def _repair_partition_at(batch, units, cut, feedback, measure):
    children = (_repair_child(batch, units[:cut], measure),
                _repair_child(batch, units[cut:], measure))
    selected = _partition_repair_feedback(feedback, children)
    if selected is None:
        return None
    return tuple(zip(children, selected))


def _repair_child(batch, units, measure):
    keys = {part.part_key for unit in units for part in unit}
    context = repair_context_paths(batch.inputs)
    return compile_batch(batch.inputs, keys,
        packing_budget(batch.packing, batch.planning_model), batch.planning_model, None,
        optional_paths=context, journal_indexes=packing_journal_indexes(measure),
        partitions=measure_partitions(measure, measure_owns_inputs(measure, batch.inputs)),
        planning_candidates=batch.planning_candidates,
        required_paths=required_context_paths(measure, keys))


def _partition_repair_feedback(feedback, children):
    selected = ([], [])
    for encoded in feedback:
        divided = _partition_correction_record(encoded, children)
        if divided is None:
            return None
        _append_partition_reviews(selected, divided)
    return tuple(tuple(records) for records in selected)


def _partition_correction_record(encoded, children):
    record = json.loads(encoded)
    if 'validation_error' in record:
        # An invalid answer has no verified source ownership to partition.
        # Keep the entire untrusted diagnostic beside each complete child.
        return tuple((encoded,) for _child in children)
    return _partition_review_record(record, children)


def _partition_review_record(record, children):
    operations = ([], [])
    for operation in record['operations']:
        index = _repair_operation_partition(operation, children)
        if index is None:
            return None
        operations[index].append(operation)
    return tuple(_partition_review_feedback(items, record['reviews']) for items in operations)


def _partition_review_feedback(operations, reviews):
    if not operations:
        return ()
    return validated_critique_feedback(operations, reviews)


def _repair_operation_partition(operation, children):
    for index, child in enumerate(children):
        if _repair_operation_present(operation, child.inputs):
            return index
    return None


def _repair_operation_present(operation, inputs):
    try:
        candidate = _reviewed_operation_for_validation(operation, inputs)
        critique_prompt(inputs, [candidate])
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _reviewed_operation_for_validation(operation, inputs):
    claims = operation.get("claims", [])
    if not claims or any("schema_version" in record for record in claims):
        return operation
    candidate = json.loads(canonical_json_bytes(operation))
    with_derived_claims([candidate], inputs)
    if len(candidate.get("claims", [])) != len(claims):
        raise ValueError("reviewed compile claim could not be reconstructed")
    return candidate

def _append_partition_reviews(selected, divided):
    for destination, records in zip(selected, divided):
        destination.extend(records)


def _apply_batch(
    batch: CompileBatch,
    resolved: ResolvedCompilePlan,
    args: argparse.Namespace,
    *,
    coordinator: MarkdownCoordinator,
    deadline: float,
    cancelled: Callable[[], bool] | None,
    owner: OwnerLease | None,
) -> BatchOutcome:
    try:
        result = apply_compile_plan(
            batch.inputs,
            resolved.plan,
            action_key=resolved.action_key,
            trigger=args.trigger,
            coordinator=coordinator,
            batch=batch,
            provider_budget=resolved.provider_budget,
            owner=_transactional_owner(coordinator, owner),
            deadline=deadline,
            cancelled=cancelled,
        )
    except (TimeoutError, IndexCapacityError):
        raise
    except CandidatesAlreadyQuarantined as already:
        return still_quarantined_outcome(already)
    except Exception as exc:  # noqa: BLE001 - no diagnostic state is a commit receipt
        return BatchOutcome(
            record_failed_batch(batch.inputs, exc, prefix="publication or source cleanup failed: ", deadline=deadline)
        )
    require_compile_active(deadline, cancelled)
    _record_batch_diagnostics(batch, result, args, coordinator, deadline=deadline)
    return committed_outcome(result)


def still_quarantined_outcome(already: CandidatesAlreadyQuarantined) -> BatchOutcome:
    """The same claims were quarantined by an earlier attempt: no commit, the daily stays pending."""
    print(
        f"compile_memory: batch still quarantined: {len(already.paths)} candidate(s) under "
        "knowledge/inbox/claims/ already await review, no page published; the daily "
        "stays pending until the candidate is reviewed."
    )
    return BatchOutcome(0, "quarantined", 0)


def _transactional_owner(
    coordinator: MarkdownCoordinator, owner: OwnerLease | None
) -> OwnerLease | None:
    """Only the database-backed coordinator understands a fenced owner lease."""
    if getattr(coordinator, "_database_contract", None) is None:
        return None
    return owner


def mirror_digests(batch: CompileBatch, coordinator: MarkdownCoordinator, *, deadline: float = math.inf) -> dict:
    """What the diagnostic mirror should say about each daily after this commit.

    Receipts are the authority. The mirror exists so cheap readers — the lint,
    the MCP status, the compile trigger — can ask "is this day compiled" without
    opening the coordinator. A long day is compiled part by part, and recording
    the last part's digest under the file name made every one of those readers
    call a fully compiled day stale for ever. The mirror now names the whole
    file, and only once every part of it carries a receipt: a quarantined batch
    writes no receipt, so it writes nothing here either (audit A-12).
    """
    compiled = receipt_predicate(coordinator, deadline=deadline)
    digests = {
        Path(item.logical_path).name: item.sha256
        for item in batch.inputs.dailies
        if _receipted_whole_snapshot(item, compiled)
    }
    digests.update(_whole_file_digests(batch.inputs.dailies, compiled))
    return digests


def _whole_file_digests(
    dailies: Sequence[DailySnapshot], compiled: Callable[[str, str], bool]
) -> dict[str, str]:
    """The file digest of every day in the batch whose every part now carries a receipt."""
    digests: dict[str, str] = {}
    for logical_path in sorted({item.logical_path for item in dailies}):
        whole = whole_daily_digest(logical_path, compiled)
        if whole is not None:
            digests[Path(logical_path).name] = whole
    return digests


def _receipted_whole_snapshot(item: DailySnapshot, compiled: Callable[[str, str], bool]) -> bool:
    """A one-part snapshot this commit compiled: the file may have grown since."""
    if item.part_count != 1:
        return False
    return snapshot_compiled(compiled, item)


def _record_batch_diagnostics(
    batch: CompileBatch,
    result: CompileApplyResult,
    args: argparse.Namespace,
    coordinator: MarkdownCoordinator,
    *, deadline: float = math.inf,
) -> None:
    hashes = mirror_digests(batch, coordinator, deadline=deadline)

    def mutate(state: dict) -> None:
        merge_compile_diagnostics(
            state,
            commit_sequence=result.commit_sequence,
            committed_at=result.committed_at,
            hashes=hashes,
            operation_id=result.operation_id,
            action_key=result.action_key,
            touched=result.touched,
            trigger=args.trigger,
        )

    update_state_under_clock(mutate, deadline)


def record_compile_source_failures(
    inputs: CompileInputs, state_root: Path, *, error_code: str, deadline: float = math.inf
) -> None:
    require_compile_active(deadline, None)
    queue = active_or_legacy_memory_queue(runtime_roots.vault_root(), state_root, deadline=deadline)
    for source in inputs.dailies:
        queue.record_source_failure(
            source.logical_path,
            source.sha256,
            error_code=utf8_prefix(error_code[:MAX_SOURCE_FAILURE_CODE_BYTES], MAX_SOURCE_FAILURE_CODE_BYTES),
            producer="compile",
            deadline=deadline,
        )


def retire_stale_source_failures(
    state_root: Path, *, coordinator: MarkdownCoordinator | None = None, deadline: float = math.inf, selection=None
) -> None:
    """Retire every failure row whose digest the file no longer has (audit B-15).

    A daily log only grows, so a failure of older bytes can never be cleared by a
    commit, and it held the day out of the archive and `run/` out of deletion
    for ever. See `docs/research/2026-09-25-a-failure-of-content-that-is-gone-is-retired.md`.
    """
    require_compile_active(deadline, None)
    queue = active_or_legacy_memory_queue(runtime_roots.vault_root(), state_root, deadline=deadline)
    coordinator = coordinator or active_or_legacy_coordinator(runtime_roots.vault_root(), state_root, deadline=deadline)
    compiled = existing_receipt_selection(selection, coordinator, deadline)
    current: dict[str, frozenset[str]] = {}
    for logical_path, digest in queue.source_failure_keys(deadline=deadline):
        require_compile_active(deadline, None)
        if logical_path not in current:
            current[logical_path] = current_source_digests(logical_path, coordinator=coordinator, deadline=deadline, selection=compiled)
        if digest not in current[logical_path]:
            queue.clear_source_failure(logical_path, digest, deadline=deadline)


def current_source_digests(
    logical_path: str, *, coordinator: MarkdownCoordinator | None = None, deadline: float = math.inf, selection=None
) -> frozenset[str]:
    """Unresolved source digests; committed receipts resolve the whole and every part."""
    require_compile_active(deadline, None)
    with open_optional_daily_source(runtime_roots.vault_root() / logical_path, deadline=deadline) as content:
        if content is None:
            return frozenset()
        return _unresolved_daily_digests(logical_path, content, coordinator, deadline, selection)


def _unresolved_daily_digests(logical_path, content, coordinator, deadline, selection):
    if coordinator is not None and daily_is_compiled(logical_path, content, existing_receipt_selection(selection, coordinator, deadline)):
        return frozenset()
    parts = {sha256_bytes(content[start:end]) for start, end in daily_part_bounds(content)}
    return frozenset({immutable_source_digest(content), *parts})


def merge_compile_diagnostics(
    state: dict[str, object],
    *,
    commit_sequence: int,
    committed_at: str,
    hashes: dict[str, str],
    operation_id: str,
    action_key: str,
    touched: tuple[str, ...],
    trigger: str,
) -> None:
    compiled = require_state_mapping(state, "compiled_daily_hashes")
    commit_versions = require_state_mapping(state, "compiled_daily_commits")
    stamp = (committed_at, commit_sequence)
    for name, digest in hashes.items():
        _merge_daily_commit(compiled, commit_versions, name, digest, stamp)
    if stamp <= _last_compile_stamp(state):
        return
    _write_compile_summary(
        state,
        stamp=stamp,
        hashes=hashes,
        operation_id=operation_id,
        action_key=action_key,
        touched=touched,
        trigger=trigger,
    )


def _merge_daily_commit(
    compiled: dict,
    commit_versions: dict,
    name: str,
    digest: str,
    stamp: tuple[str, int],
) -> None:
    """Keep the newest commit for one day; a replayed older commit must not win."""
    if stamp <= _previous_stamp(commit_versions.get(name)):
        return
    compiled[name] = digest
    commit_versions[name] = {"committed_at": stamp[0], "sequence": stamp[1]}


def _previous_stamp(previous: object) -> tuple[str, int]:
    if not isinstance(previous, dict):
        return ("", -1)
    return (
        _state_text(previous.get("committed_at")),
        _state_sequence(previous.get("sequence")),
    )


def _last_compile_stamp(state: dict[str, object]) -> tuple[str, int]:
    return (
        _state_text(state.get("last_compile_committed_at")),
        _state_sequence(state.get("last_compile_commit_sequence")),
    )


def _state_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value


def _state_sequence(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return -1
    return value


def _write_compile_summary(
    state: dict[str, object],
    *,
    stamp: tuple[str, int],
    hashes: dict[str, str],
    operation_id: str,
    action_key: str,
    touched: tuple[str, ...],
    trigger: str,
) -> None:
    state["last_compile_commit_sequence"] = stamp[1]
    state["last_compile_committed_at"] = stamp[0]
    state["last_compile_at"] = stamp[0]
    state["last_compile_trigger"] = trigger
    state["last_compiled_files"] = sorted(hashes)
    state["last_compiled_touched"] = list(touched)
    state["last_index_rebuild_ok"] = True
    state["last_compile_action_key"] = action_key
    state["last_compile_operation_id"] = operation_id
