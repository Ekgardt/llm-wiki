"""The compile's required context, ranking, measurement and batch packing."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from itertools import groupby
from pathlib import Path
from typing import TYPE_CHECKING

from bounded_io import (  # noqa: E402
    read_stable_bytes,
)
from compile_cache import (  # noqa: E402
    CompileCache,
)
from compile_receipts import DailySnapshot, SourceSnapshot, iter_native_part_units, physical_source
from context_budget import (  # noqa: E402
    ContextBudget,
    TokenCounter,  # noqa: E402
)
from evidence_resolver import (  # noqa: E402
    MAX_DAILY_PART_BYTES,  # noqa: F401 - re-exported: callers read the writer's bound here
    EvidenceRef,
    EvidenceResolver,
    evidence_candidates,
)
from llm_client import (  # noqa: E402
    codex_basis_digest,
    codex_basis_file_digest,
    count_planning_input,
    forced_provider,
    provider_candidates,
    provider_environment,
    resolve_codex_planning_basis,
    worst_case_call_seconds,
)
from llm_wiki.compile_receipt_authority import (
    MAX_RECEIPT_BYTES,
    require_transaction_authority,
)
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes  # noqa: E402
from llm_wiki.formats.receipts import (  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402  # noqa: E402
    corrupt_receipt,
    parse_compile_receipt_v2,
)
from markdown_transaction import (  # noqa: E402
    MarkdownCoordinator,
)

if TYPE_CHECKING:
    pass
from llm_wiki import runtime_roots
from llm_wiki.compile_pipeline_base import DRAFT_SYSTEM, SOURCE_CHOICE_PARSING, compile_budget
from llm_wiki.compile_pipeline_operations import record_measured_projection
from llm_wiki.compile_pipeline_prompts import (
    BatchTextLookup,
    ByteBatchMeasure,
    CompileAttempt,
    assert_external_work_allowed,
    batch_text,
    bounded_attempt_budget,
    draft_layout,
    fitting_measured_context,
    mandatory_planning_window,
    measure_choice_resolution,
    measured_choice_layout_needed,
    packing_budget,
    require_ready_compile_batch,
    required_context_paths,
    with_required_context,
)
from llm_wiki.compile_pipeline_sources import (
    CompileBatch,
    CompileInputs,
    CompilePackingIdentity,
    DailyPartitionProofs,
    NativeJournalIndexes,
    ResolvedCompilePlan,
    inputs_with_parts,
    join_partition,
    measurement_proofs,
    native_part_units,
    report_stage_detail,
    require_join_member,
    require_native_unit,
    same_day_parts_join,
    select_snapshot_paths,
    snapshot_compile_inputs,
    snapshot_paths,
    source_descriptor,
    subset_compile_inputs,
)


def _iter_admitted_compile_units(units, budget, prepare, on_refused):
    """Report complete refused units before advancing; never retain their bodies."""
    for unit in units:
        inputs, measure = prepare(unit)
        if unit_is_refused(unit, budget, measure):
            on_refused(inputs_with_parts(inputs, tuple(unit)))
            del inputs, measure
            continue
        del inputs, measure
        yield unit


def _partition_compile_units(inputs, budget, measure):
    refused = []
    units = _iter_admitted_compile_units(
        native_part_units(inputs.dailies), budget, lambda _unit: (inputs, measure), refused.append,
    )
    accepted = tuple(part for unit in units for part in unit)
    return inputs_with_parts(inputs, accepted), tuple(refused)


def partition_packable(
    inputs: CompileInputs,
    *,
    model: str | None,
    token_adapters: Mapping[str, TokenCounter] | None = None,
    planning_candidates: tuple | None = None,
) -> tuple[CompileInputs, tuple[CompileInputs, ...]]:
    """Admit independent atomic units; retain each refused unit for durable failure reporting."""
    budget = compile_budget(model)
    measure = batch_measure(inputs, model, token_adapters)
    attach_required_context(measure, inputs, planning_candidates)
    return _partition_compile_units(inputs, budget, measure)


def pack_compile_batches(
    inputs: CompileInputs,
    *,
    model: str | None,
    token_adapters: Mapping[str, TokenCounter] | None = None,
    budget: ContextBudget | None = None,
    planning_candidates: tuple | None = None,
) -> tuple[CompileBatch, ...]:
    return pack_planned_compile_batches(inputs, model=model, token_adapters=token_adapters,
                                 budget=budget, planning_candidates=planning_candidates)


def pack_planned_compile_batches(inputs, *, model, token_adapters=None, budget=None,
                          planning_candidates=None, context_pending=False):
    return tuple(iter_compile_batches(inputs, model=model, token_adapters=token_adapters,
                                      budget=budget, planning_candidates=planning_candidates,
                                      context_pending=context_pending))


def iter_compile_batches(inputs, *, model, token_adapters=None, budget=None,
                         planning_candidates=None, context_pending=False):
    """Yield batches without retaining completed batches; errors arise on iteration."""
    budget = _validated_packing_budget(budget, model)
    measure = batch_measure(inputs, model, token_adapters)
    attach_required_context(measure, inputs, planning_candidates)
    ranking = _packing_context_ranking(inputs, context_pending)
    for paths in _iter_daily_groups(inputs, budget, measure):
        yield _compile_measured_batch(inputs, paths, budget, model, token_adapters,
                                      measure, ranking, planning_candidates, context_pending)


def _compile_measured_batch(inputs, paths, budget, model, adapters, measure, ranking,
                            candidates, context_pending):
    return compile_batch(
        inputs, paths, budget, model, adapters,
        optional_paths=_packing_context_paths(inputs, paths, ranking, budget, measure),
        journal_indexes=packing_journal_indexes(measure),
        partitions=measure_partitions(measure, measure_owns_inputs(measure, inputs)),
        planning_candidates=candidates, context_pending=context_pending,
        required_paths=required_context_paths(measure, paths),
    )


def iter_streamed_compile_batches(records, context_inputs, required_context, *, model,
                                  token_adapters=None, budget=None, planning_candidates=None,
                                  context_pending=False, on_refused=None):
    """Plan current units against global context without retaining completed days."""
    budget = _validated_packing_budget(budget, model)
    ranking = _packing_context_ranking(context_inputs, context_pending)
    _report_required_context(required_context)
    prepare = partial(_stream_batch_measure, context_inputs, required_context,
                      model, token_adapters, planning_candidates)
    units = iter_native_part_units(records)
    if on_refused is not None:
        units = _iter_admitted_compile_units(units, budget, prepare, on_refused)
    for paths, measure in _iter_measured_groups(units, budget, prepare):
        yield _compile_measured_batch(measure.inputs, paths, budget, model, token_adapters,
                                      measure, ranking, planning_candidates, context_pending)


def _stream_batch_measure(context_inputs, required, model, adapters, candidates, parts):
    inputs = replace(context_inputs, dailies=tuple(parts))
    measure = batch_measure(inputs, model, adapters)
    measure.required_context = required
    measure.planning_candidates = candidates
    return inputs, measure


@dataclass(frozen=True)
class _ContextPartIdentity:
    index: int
    part_key: str
    logical_path: str
    sha256: str
    original_sha256: str
    byte_start: int
    byte_end: int
    has_original: bool


def _context_part_identity(index, part):
    return _ContextPartIdentity(index, part.part_key, part.logical_path, part.sha256,
                                part.original_sha256, part.byte_start, part.byte_end,
                                part.original_content is not None)


def _require_context_identity(identities, index, part):
    if index >= len(identities) or _context_part_identity(index, part) != identities[index]:
        raise ValueError('daily citation identity changed between passes')


def _context_day_partitions(measure):
    partitions = getattr(measure, 'partitions', None)
    if partitions is not None:
        return partitions
    return DailyPartitionProofs(NativeJournalIndexes())


class RequiredCitationContext:
    """Global citation identity, with physical proofs scoped to each daily pass."""

    def __init__(self, inputs, measure, *, daily_records=None):
        records = daily_records or partial(iter, inputs.dailies)
        identities = tuple(_context_part_identity(index, part)
                           for index, part in enumerate(records()))
        self.parts = _context_reference_parts(identities)
        self.dates = {key[0] for key in self.parts}
        self.paths = {}
        self.unresolved = 0
        self.pending = {}
        for source in _existing_context_pages(inputs):
            self._page(source)
        self._resolve_records(records(), identities, measure)

    def _resolve_records(self, records, identities, measure):
        count = 0
        for _, day in groupby(enumerate(records), key=lambda row: row[1].logical_path):
            count += self._resolve_day(day, identities, measure)
        if count != len(identities):
            raise ValueError('daily citation count changed between passes')

    def _resolve_day(self, day, identities, measure):
        resolver = EvidenceResolver(runtime_roots.vault_root())
        partitions = _context_day_partitions(measure)
        count = 0
        for index, part in day:
            _require_context_identity(identities, index, part)
            self._resolve_part(index, part, resolver, partitions)
            count += 1
        return count

    def _resolve_part(self, index, part, resolver, partitions):
        for path, reference, original in self.pending.pop(index, ()):
            self._associate(path, reference, part, original, resolver, partitions)

    def _page(self, source):
        if sha256_bytes(source.content) != source.sha256:
            raise ValueError("required context snapshot hash differs from its bytes")
        for reference in evidence_candidates(source.content.decode('utf-8', errors='strict')):
            self._reference(source.logical_path, reference)

    def _reference(self, path, reference):
        if not isinstance(reference, EvidenceRef):
            self.unresolved += 1
            return
        parts = self.parts.get((reference.daily_id, reference.source_sha256), ())
        self.unresolved += int(not parts and reference.daily_id in self.dates)
        for part, original in parts:
            self.pending.setdefault(part.index, []).append((path, reference, original))

    def _associate(self, path, reference, part, original, resolver, partitions):
        content = physical_source(part).content if original else part.content
        try:
            resolver.resolve_bytes(reference, content, source_path=runtime_roots.vault_root() / part.logical_path,
                                   reuse_immutable=True)
        except ValueError:
            self.unresolved += 1
            return
        start, end = _context_reference_bounds(part, reference, original)
        if part.byte_start < end and part.byte_end > start:
            _require_context_part(part, partitions)
            self.paths.setdefault(part.part_key, set()).add(path)

    def for_paths(self, paths):
        return set().union(*(self.paths.get(path, set()) for path in paths))


def _existing_context_pages(inputs):
    wanted = {path for path in snapshot_paths(inputs.sources)
              if path.startswith('knowledge/notes/') and path.endswith('.md')}
    return iter(select_snapshot_paths(inputs.sources, wanted))


def _context_reference_parts(parts):
    indexed = {}
    for part in parts:
        _index_context_part(indexed, part)
    return {key: _unique_context_origins(values) for key, values in indexed.items()}


def _unique_context_origins(parts):
    origins = {(part.logical_path, part.original_sha256, part.byte_start, part.byte_end)
               for part, original in parts if not original}
    if len(origins) > 1:
        return ()
    return parts


def _index_context_part(indexed, part):
    date = Path(part.logical_path).stem
    if part.logical_path != f"knowledge/daily/{date}.md":
        return
    physical_digest = part.original_sha256 if part.has_original else part.sha256
    indexed.setdefault((date, physical_digest), []).append((part, True))
    if part.has_original and part.sha256 != physical_digest:
        indexed.setdefault((date, part.sha256), []).append((part, False))


def _context_reference_bounds(part, reference, original):
    if original or part.original_content is None:
        return reference.byte_start, reference.byte_end
    return part.byte_start + reference.byte_start, part.byte_start + reference.byte_end


def _require_context_part(part, partitions):
    if part.original_content is None:
        if sha256_bytes(part.content) != part.sha256:
            raise ValueError("required context source part hash differs")
        return
    bounds, _native = join_partition(part, partitions)
    require_join_member(part, bounds)


def attach_required_context(measure, inputs, candidates):
    measure.required_context = RequiredCitationContext(inputs, measure)
    measure.planning_candidates = candidates
    _report_required_context(measure.required_context)


def _report_required_context(required):
    if required.unresolved:
        report_stage_detail('context', 'unresolved_association', str(required.unresolved))


def _required_selection_budget(measure, paths, target):
    required = required_context_paths(measure, paths)
    measured = measure(paths, required)
    candidates = getattr(measure, 'planning_candidates', None)
    return complete_source_budget(target, measured, required, candidates)


def complete_source_budget(target, measured, required, candidates):
    window = mandatory_planning_window(candidates, target.model)
    if window is None:
        return _mandatory_context_budget(target, measured, required, candidates)
    bounded = bounded_attempt_budget(target, window)
    return _expanded_required_budget(bounded, measured, window)


def _source_grouping_budget(target, measure):
    window = _measure_planning_window(target, measure)
    if window is None:
        return target
    if window <= target.reserved_output_tokens + target.safety_margin_tokens:
        return target
    return replace(target, max_input_tokens=window)


def _measure_planning_window(target, measure):
    candidates = getattr(measure, 'planning_candidates', None)
    if not candidates:
        return None
    return mandatory_planning_window(candidates, target.model)


def _mandatory_context_budget(target, measured, required, candidates):
    if not required:
        return target
    window = mandatory_planning_window(candidates, target.model)
    target = bounded_attempt_budget(target, window)
    if measured <= target.available_input_tokens:
        return target
    return _expanded_required_budget(target, measured, window)


def _expanded_required_budget(target, measured, window):
    minimum = measured + target.reserved_output_tokens + target.safety_margin_tokens
    if minimum <= target.max_input_tokens:
        return target
    if window is None or minimum > window:
        return target
    return replace(target, max_input_tokens=minimum)


def _unit_admission_budget(unit, target, count, measure):
    paths = {part.part_key for part in unit}
    required = required_context_paths(measure, paths)
    candidates = getattr(measure, 'planning_candidates', None)
    if _measure_planning_window(target, measure) is not None:
        return complete_source_budget(target, count, required, candidates)
    if required:
        return _mandatory_context_budget(target, count, required,
                                         getattr(measure, 'planning_candidates', None))
    return atomic_unit_budget(unit, target, count,
                              partitions=measure_partitions(measure, type(measure) in (ByteBatchMeasure, TokenBatchMeasure)))


def _require_mandatory_count(count, budget, required):
    if required and count > budget.available_input_tokens:
        raise ValueError('complete source and required context exceed qualified planning budget')


def _packing_context_ranking(inputs, context_pending):
    if context_pending:
        return None
    daily_paths = {item.logical_path for item in inputs.dailies}
    optional = select_snapshot_paths(inputs.sources, set(snapshot_paths(inputs.sources)) - daily_paths)
    return ContextRanking(optional)


def _packing_context_paths(inputs, paths, ranking, budget, measure):
    if ranking is None:
        return required_context_paths(measure, paths)
    selected_budget = _required_selection_budget(measure, paths, budget)
    return fitting_context(paths, ranking.ordered(measure_batch_text(inputs, paths, measure)),
                            selected_budget, measure)


def _validated_packing_budget(budget, model):
    selected = budget or compile_budget(model)
    if selected.model != model:
        raise ValueError("compile packing model disagrees with its budget")
    return selected


def packing_journal_indexes(measure):
    if type(measure) is ByteBatchMeasure:
        return measure.journal_indexes
    return None


_RANKING_WORD = re.compile(r"[^\W\d_]{4,}")
# BM25's usual constants; see ContextRanking.
BM25_K1 = 1.2
BM25_B = 0.75


def _words(content: bytes) -> frozenset[str]:
    return frozenset(_RANKING_WORD.findall(content.decode("utf-8", errors="ignore").casefold()))


def measure_batch_text(inputs, paths, measure):
    if measure_owns_inputs(measure, inputs):
        return measure.batch_texts.text(inputs, paths)
    return batch_text(inputs, paths)


def _inverse_document_frequency(documents, total: int) -> dict[str, float]:
    """BM25's IDF: `ln((N - n + 0.5) / (n + 0.5) + 1)` for each word."""
    frequency: dict[str, int] = {}
    for words in documents:
        for word in words:
            frequency[word] = frequency.get(word, 0) + 1
    return {word: math.log((total - count + 0.5) / (count + 0.5) + 1) for word, count in frequency.items()}


def _length_factors(words_by_path: Mapping[str, frozenset[str]]) -> dict[str, float]:
    """BM25's length normalisation with a term frequency of one."""
    lengths = [len(words) for words in words_by_path.values()]
    average = (sum(lengths) / len(lengths)) if lengths else 1.0
    return {
        path: (BM25_K1 + 1) / (1 + BM25_K1 * (1 - BM25_B + BM25_B * len(words) / max(average, 1.0)))
        for path, words in words_by_path.items()
    }


class ContextRanking:
    """Optional context in order of relevance to a batch's days, not by path.

    A 32k window shows a fraction of the notes; offered by path, the planner saw
    the alphabetically early pages and created a new page beside the one its day
    was about (audit B-12, B-13). The score is the BM25 IDF of the words a page
    shares with the days; ties go by path, so the order is deterministic. See
    `docs/research/2026-09-25-the-compile-sees-the-pages-its-day-is-about.md`.
    """

    def __init__(self, sources: Sequence[SourceSnapshot]) -> None:
        self.sources = sources
        self.paths = snapshot_paths(sources)
        self.words = {item.logical_path: _words(item.content) for item in self.sources}
        self.idf = _inverse_document_frequency(self.words.values(), len(self.sources))
        self.length_factor = _length_factors(self.words)

    def ordered(self, day_text: bytes) -> Sequence[SourceSnapshot]:
        day = _words(day_text)
        indices = sorted(range(len(self.paths)), key=lambda index: (
            -self._score(self.paths[index], day), self.paths[index]))
        return _RankedSources(self.sources, indices)

    def _score(self, path: str, day: frozenset[str]) -> float:
        """BM25 with every shared word counted once: a long page does not win by length."""
        shared = sum(self.idf[word] for word in self.words[path] & day)
        return shared * self.length_factor[path]


class _RankedSources(Sequence):
    """An ordering of a snapshot collection, without retaining decoded records."""

    def __init__(self, sources, indices):
        self.sources, self.indices = sources, tuple(indices)

    def __len__(self):
        return len(self.indices)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return _RankedSources(self.sources, self.indices[index])
        return self.sources[self.indices[index]]

    def logical_paths(self):
        paths = snapshot_paths(self.sources)
        return tuple(paths[index] for index in self.indices)


def draft_prompt_count(inputs, model, adapters):
    prompt, schema = draft_layout(inputs)
    return count_planning_input(prompt, DRAFT_SYSTEM, schema, model=model, adapters=adapters)


def batch_measure(
    inputs: CompileInputs,
    model: str | None,
    token_adapters: Mapping[str, TokenCounter] | None,
) -> Callable[..., int]:
    """Count the draft-prompt tokens one candidate grouping would cost."""

    if not _has_compile_tokenizer(model, token_adapters):
        return ByteBatchMeasure(inputs)

    return TokenBatchMeasure(inputs, model, token_adapters)


def _has_compile_tokenizer(model, adapters):
    if model is None:
        return False
    if adapters is None:
        return False
    return model in adapters


class TokenBatchMeasure:
    """A tokenizer counts the exact serialization of its captured inputs."""

    def __init__(self, inputs, model, adapters):
        self.inputs, self.model, self.adapters = inputs, model, adapters
        self.batch_texts = BatchTextLookup(inputs)
        self.journal_indexes, self.partitions = measurement_proofs(inputs)
        self.choice_resolver = None
        self.choice_bindings = {}

    def __call__(self, paths, optional_paths=None):
        optional_paths = with_required_context(self, paths, optional_paths)
        subset = subset_compile_inputs(self.inputs, paths, optional_paths, partitions=self.partitions, journal_indexes=self.journal_indexes)
        record_measured_projection(self.choice_bindings, subset.dailies, subset.sources)
        count = measure_token_subset(self, subset)
        if count.tokens is None:
            raise ValueError("compile input token count is unknown")
        return count.tokens


def measure_owns_inputs(measure, inputs):
    return type(measure) in (ByteBatchMeasure, TokenBatchMeasure) and measure.inputs is inputs


def measure_token_subset(measure, subset):
    if not measured_choice_layout_needed(measure, subset.dailies, subset.sources):
        return draft_prompt_count(subset, measure.model, measure.adapters)
    with measure_choice_resolution(measure):
        return draft_prompt_count(subset, measure.model, measure.adapters)


def group_dailies(inputs: CompileInputs, budget: ContextBudget,
                   measure: Callable[..., int]) -> list[set[str]]:
    return list(_iter_daily_groups(inputs, budget, measure))


def _iter_daily_groups(inputs, budget, measure):
    def prepare(_parts):
        return inputs, measure

    for paths, _measure in _iter_measured_groups(iter_native_part_units(inputs.dailies), budget, prepare):
        yield paths


class _CompileGroup:
    def __init__(self):
        self.paths = set()
        self.days = set()
        self.parts = []

    def starts_new(self, unit, budget, inputs, measure):
        source_bound = measure_owns_inputs(measure, inputs)
        return _starts_a_new_group(self.paths, self.days, unit, budget, measure,
                                   current_parts=self.parts,
                                   partitions=measure_partitions(measure, source_bound),
                                   source_bound=source_bound)

    def add(self, unit):
        self.parts.extend(unit)
        self.paths.update(part.part_key for part in unit)
        self.days.add(unit[0].logical_path)


def _iter_measured_groups(units, budget, prepare):
    current = _CompileGroup()
    for unit in units:
        inputs, measure = prepare((*current.parts, *unit))
        _require_unit_fits(unit, budget, measure)
        if current.starts_new(unit, budget, inputs, measure):
            yield current.paths, measure
            current = _CompileGroup()
            inputs, measure = prepare(unit)
        current.add(unit)
    if current.paths:
        yield current.paths, measure


def _starts_a_new_group(
    current: set[str],
    days: set[str],
    unit: Sequence[DailySnapshot],
    budget: ContextBudget,
    measure: Callable[..., int],
    *, current_parts=(), partitions=None, source_bound=False,
) -> bool:
    """Close a full group or an unverified same-day join."""
    if not current:
        return False
    if unit[0].logical_path in days and not _qualified_same_day_join(current_parts, unit, partitions, source_bound):
        return True
    admitted = _source_grouping_budget(budget, measure)
    return measure(current | {part.part_key for part in unit}) > admitted.available_input_tokens


def measure_partitions(measure, source_bound):
    if source_bound:
        return measure.partitions
    return None


def _qualified_same_day_join(parts, unit, partitions, source_bound):
    if not source_bound:
        return False
    return same_day_parts_join(parts, unit, partitions)


def _require_unit_fits(unit, budget, measure):
    if not unit_is_refused(unit, budget, measure):
        return
    raise ValueError("daily source exceeds compile input budget")


def unit_is_refused(unit, budget, measure):
    count = measure({part.part_key for part in unit})
    admitted = _unit_admission_budget(unit, budget, count, measure)
    return count > admitted.available_input_tokens


def atomic_unit_budget(unit, target, measured, *, partitions=None):
    if len(unit) <= 1:
        return target
    if measured <= target.available_input_tokens:
        return target
    require_native_unit(unit, partitions=partitions)
    minimum = measured + target.reserved_output_tokens + target.safety_margin_tokens
    return replace(target, max_input_tokens=max(target.max_input_tokens, minimum))


def fitting_context(
    paths: set[str],
    optional_sources: Sequence[SourceSnapshot],
    budget: ContextBudget,
    measure: Callable[..., int],
) -> set[str]:
    """Carry optional context pages while they still fit beside the days."""
    if type(measure) is ByteBatchMeasure:
        return measure.fitting_context(paths, optional_sources, budget)
    return fitting_measured_context(paths, optional_sources, budget, measure)


def compile_batch(
    inputs: CompileInputs,
    paths: set[str],
    budget: ContextBudget,
    model: str | None,
    token_adapters: Mapping[str, TokenCounter] | None,
    *,
    optional_paths: set[str] | None = None,
    journal_indexes=None,
    partitions=None,
    planning_candidates=None,
    context_pending=False,
    required_paths=None,
) -> CompileBatch:
    subset = subset_compile_inputs(inputs, paths, optional_paths, journal_indexes=journal_indexes, partitions=partitions)
    count = count_with_partition_proofs(subset, model, token_adapters, journal_indexes, partitions)
    if count.tokens is None or count.source not in {"tokenizer", "estimated"}:
        raise ValueError("compile input token count is unknown")
    budget = _final_context_budget(subset, budget, count.tokens, required_paths, planning_candidates, partitions)
    _require_mandatory_count(count.tokens, budget, required_paths)
    manifest = tuple(sorted(source_descriptor(item) for item in subset.dailies))
    manifest_bytes = canonical_json_bytes(
        [item.receipt_descriptor() for item in manifest]
    )
    packing = CompilePackingIdentity(
        algorithm=packing_algorithm(subset),
        tokenizer_identity=_tokenizer_identity(count.source, model),
        count_source=count.source,
        max_input_tokens=budget.max_input_tokens,
        reserved_output_tokens=budget.reserved_output_tokens,
        safety_margin_tokens=budget.safety_margin_tokens,
        measured_input_tokens=count.tokens,
    )
    return CompileBatch(subset, manifest, sha256_bytes(manifest_bytes), packing,
                        model, planning_candidates, context_pending, _required_context_tuple(required_paths))


def count_with_partition_proofs(subset, model, adapters, journal_indexes, partitions):
    """Reuse immutable parsing while canonical projection and heads verify afresh."""
    token = SOURCE_CHOICE_PARSING.set((journal_indexes, partitions))
    try:
        return draft_prompt_count(subset, model, adapters)
    finally:
        SOURCE_CHOICE_PARSING.reset(token)


def _required_context_tuple(paths):
    return tuple(sorted(paths or ()))


def _final_context_budget(subset, target, measured, required, candidates, partitions):
    if mandatory_planning_window(candidates, target.model) is not None:
        return complete_source_budget(target, measured, required, candidates)
    if required:
        return _mandatory_context_budget(target, measured, required, candidates)
    return _selected_atomic_budget(subset, target, measured, partitions=partitions)


def _selected_atomic_budget(inputs, target, measured, *, partitions=None):
    units = native_part_units(inputs.dailies)
    if any(len(unit) > 1 for unit in units):
        return atomic_unit_budget(max(units, key=len), target, measured, partitions=partitions)
    return target


def packing_algorithm(inputs):
    paths = [part.logical_path for part in inputs.dailies]
    if len(paths) != len(set(paths)):
        return "compile-complete-items/v2"
    return "compile-complete-items/v1"


def _tokenizer_identity(count_source: str, model: str | None) -> str:
    if count_source == "tokenizer":
        return f"adapter:{model}"
    return "utf8-byte-estimate/v1"


def refresh_compile_batch(batch: CompileBatch, *, deadline: float = math.inf,
                           retained_context: bool = False) -> CompileBatch:
    refreshed = _fresh_compile_batch_inputs(batch)
    return _refresh_compile_batch_inputs(batch, refreshed, deadline, retained_context)


def refresh_compile_batch_from_context(batch, context, *, deadline=math.inf, retained_context=False):
    """Use exact caller-owned context with the ordinary semantic refresh checks."""
    refreshed = compile_batch_context_inputs(batch, context)
    return _refresh_compile_batch_inputs(batch, refreshed, deadline, retained_context)


def _refresh_compile_batch_inputs(batch, refreshed, deadline, retained_context):
    candidates = _refreshed_batch_candidates(batch, deadline)
    batches = _refreshed_context_batches(batch, refreshed, candidates, retained_context)
    if len(batches) != 1 or batches[0].manifest != batch.manifest:
        raise ValueError("compile batch changed while refreshing context")
    _require_refresh_identity(batch, batches[0], candidates)
    return batches[0]


def _fresh_compile_batch_inputs(batch):
    return compile_batch_context_inputs(batch, snapshot_compile_inputs(()))


def compile_batch_context_inputs(batch, context):
    daily_sources = tuple(SourceSnapshot(item.logical_path, item.content, item.sha256)
                          for item in batch.inputs.dailies)
    sources = _combined_context_sources((daily_sources, context.sources))
    return CompileInputs(batch.inputs.dailies, sources, context.targets, context.vault_files)


def _combined_context_sources(groups):
    positions = []
    for group, records in enumerate(groups):
        positions.extend((path, group, index) for index, path in enumerate(snapshot_paths(records)))
    return _CombinedSources(groups, tuple(sorted(positions)))


class _CombinedSources(Sequence):
    """Merge source metadata while retaining no decoded archive context bodies."""

    def __init__(self, groups, positions):
        self.groups, self.positions = groups, positions

    def __len__(self):
        return len(self.positions)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return _CombinedSources(self.groups, self.positions[index])
        _path, group, position = self.positions[index]
        return self.groups[group][position]

    def logical_paths(self):
        return tuple(position[0] for position in self.positions)

    def select_paths(self, wanted):
        return _CombinedSources(self.groups, tuple(pos for pos in self.positions if pos[0] in wanted))


def _refreshed_context_batches(batch, refreshed, candidates, retained_context):
    if retained_context:
        return (_refresh_retained_context_batch(batch, refreshed, candidates),)
    return pack_compile_batches(refreshed, model=batch.planning_model,
        budget=packing_budget(batch.packing, batch.planning_model), planning_candidates=candidates)


def _refresh_retained_context_batch(batch, inputs, candidates):
    require_repair_context(batch, replace(batch, inputs=inputs))
    measure = batch_measure(inputs, batch.planning_model, None)
    attach_required_context(measure, inputs, candidates)
    keys = {part.part_key for part in inputs.dailies}
    context = repair_context_paths(batch.inputs) | required_context_paths(measure, keys)
    return compile_batch(inputs, keys,
        packing_budget(batch.packing, batch.planning_model), batch.planning_model, None,
        optional_paths=context, journal_indexes=packing_journal_indexes(measure),
        partitions=measure_partitions(measure, measure_owns_inputs(measure, inputs)),
        planning_candidates=candidates, required_paths=context)


def _require_refresh_identity(previous, refreshed, candidates):
    _require_refreshed_required_context(previous, refreshed)
    if previous.planning_model != refreshed.planning_model or candidates is not refreshed.planning_candidates:
        raise ValueError("compile model changed while refreshing context")
    before = packing_budget(previous.packing, previous.planning_model)
    after = packing_budget(refreshed.packing, refreshed.planning_model)
    expected = _final_context_budget(refreshed.inputs, before,
        refreshed.packing.measured_input_tokens, refreshed.required_context_paths, candidates, None)
    if expected != after:
        raise ValueError("compile budget changed while refreshing context")


def _require_refreshed_required_context(previous, refreshed):
    if not set(previous.required_context_paths).issubset(refreshed.required_context_paths):
        raise ValueError('required citation context was lost while refreshing')


def _refreshed_batch_candidates(batch, deadline):
    candidates = batch.planning_candidates
    if candidates is None:
        return None
    refreshed = tuple(_refreshed_batch_candidate(candidate, deadline) for candidate in candidates)
    return candidates if all(old is new for old, new in zip(candidates, refreshed)) else refreshed


def _refreshed_batch_candidate(candidate, deadline):
    basis = getattr(candidate, "_codex_basis", None)
    if basis is None:
        return candidate
    if _batch_provider_basis_current(basis):
        return candidate
    refreshed = planned_candidate(basis.original_descriptor, deadline)
    _require_refreshed_batch_candidate(candidate, refreshed)
    return refreshed


def _batch_provider_basis_current(basis):
    if codex_basis_digest(provider_environment()) != basis.environment_sha256:
        return False
    return all(codex_basis_file_digest(path) == expected for path, expected in basis.config_files)


def _require_refreshed_batch_candidate(previous, current):
    if current.resolution_failure is not None:
        raise ValueError(f"compile provider revalidation failed: {current.resolution_failure}")
    if _batch_provider_identity(previous) != _batch_provider_identity(current):
        raise ValueError("compile batch provider, model or settings changed during revalidation")


def _batch_provider_identity(candidate):
    basis = getattr(candidate, "_codex_basis", None)
    return (candidate.provider, candidate.model, dict(candidate.inference_settings),
            candidate.fallback_from, getattr(basis, "model_provider", None))


def _receipt_path(digest: str) -> Path:
    return runtime_roots.daily_dir() / "receipts" / f"{digest}.md"


def read_compile_receipt_v2(
    digest: str,
    coordinator: MarkdownCoordinator,
    *,
    path: Path | None = None,
    vault: Path | None = None,
) -> dict[str, object] | None:
    path = _receipt_path(digest) if path is None else Path(path)
    vault = runtime_roots.vault_root() if vault is None else Path(vault)
    try:
        raw_bytes = read_stable_bytes(path, MAX_RECEIPT_BYTES, label="compile receipt")
    except FileNotFoundError:
        return None
    try:
        record = parse_compile_receipt_v2(raw_bytes, digest)
        require_transaction_authority(record, coordinator, path, vault, raw_bytes)
        return record
    except (
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise corrupt_receipt(exc, path) from exc


# Historical compatibility only. Selection and archive authority use v3 readers.
read_compile_receipt = read_compile_receipt_v2


def resolve_compile_plan(
    inputs: CompileInputs,
    cache: CompileCache,
    *,
    coordinator: MarkdownCoordinator,
    batch: CompileBatch | None = None,
    token_adapters: Mapping[str, TokenCounter] | None = None,
    critic_feedback: tuple[str, ...] = (),
    call_runner=None,
) -> ResolvedCompilePlan:
    """Resolve a validated semantic plan without entering the writer gate."""
    require_ready_compile_batch(batch)
    assert_external_work_allowed(coordinator)
    if batch is not None and batch.inputs != inputs:
        raise ValueError("compile batch inputs disagree")
    attempt = CompileAttempt(inputs, cache, batch, token_adapters, call_runner=call_runner)
    attempt.critic_feedback = critic_feedback
    resolved = _first_resolved_plan(attempt)
    if resolved is None:
        raise unresolved_compile_error(attempt)
    return resolved


class CompileRepairCapacityError(RuntimeError):
    """Draft or review work that cannot fit beside its complete source group."""

    def __init__(self, message, feedback):
        super().__init__(message)
        self.feedback = feedback


class CompilePlanUnavailable(RuntimeError):
    """Resolution exhausted its known outcomes without a validated plan.

    Independent source work may continue after recording this failure. Generic
    runtime, storage, ownership and dependency errors are not this outcome.
    """


def unresolved_compile_error(attempt):
    message = _no_plan_message(attempt.lineage)
    capacity = any(item.endswith(':input_budget') for item in attempt.lineage)
    if capacity:
        return CompileRepairCapacityError(message, attempt.critic_feedback)
    return CompilePlanUnavailable(message)


def _first_resolved_plan(attempt: CompileAttempt) -> ResolvedCompilePlan | None:
    """The first provider that answers with a plan; a timeout ends the chain.

    A deadline is the budget of the whole compile call, so the next provider
    would spend a second one the step was never given. `chain_stops_after` is the
    one rule all three provider chains of the product ask.
    """
    for candidate in compile_candidate_chain(attempt.batch):
        resolved = attempt.resolve(candidate)
        if resolved is not None:
            return resolved
        if attempt.out_of_time:
            return None
    return None


def compile_candidate_chain(batch):
    if batch is None or batch.planning_candidates is None:
        return provider_candidates(forced_provider(), max_tokens=4000)
    return batch.planning_candidates


def _no_plan_message(lineage: Sequence[str]) -> str:
    """Say which provider failed at which stage, not merely that none worked.

    The chain records `stage:provider:code` for every attempt and used to drop
    it on the floor, so a live vault that could not compile reported the same
    sentence whether no provider existed, one refused, or a plan failed its
    critique.
    """
    if not lineage:
        return "no LLM provider produced a validated compile plan: none was tried"
    return (
        "no LLM provider produced a validated compile plan: " + "; ".join(lineage)
    )


def planned_candidate(candidate, deadline):
    if candidate.provider != "codex" or candidate.resolution_failure is not None:
        return candidate
    try:
        limit = min(deadline, time.monotonic() + worst_case_call_seconds("codex"))
        basis = resolve_codex_planning_basis(candidate, deadline=limit)
    except Exception as error:  # noqa: BLE001 - preserve failed provider in the fallback lineage
        return _failed_planning_candidate(candidate, error)
    return basis.descriptor if basis is not None else candidate


def _failed_planning_candidate(candidate, error):
    failure = "provider_error"
    if isinstance(error, TimeoutError):
        failure = "provider_timeout"
    report_stage_detail("planning", failure, type(error).__name__)
    return replace(candidate, _resolution_failure=failure)


def require_repair_context(previous, refreshed):
    context = repair_context_paths(previous.inputs)
    if not context.issubset(source.logical_path for source in refreshed.inputs.sources):
        raise ValueError('complete selected context was lost while repartitioning repair work')


def repair_context_paths(inputs):
    daily_paths = {part.logical_path for part in inputs.dailies}
    return {source.logical_path for source in inputs.sources if source.logical_path not in daily_paths}
