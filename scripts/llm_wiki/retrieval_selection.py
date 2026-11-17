"""Evidence ordering, aspect coverage and page diversity of the answer window."""
from __future__ import annotations

import re
import sqlite3
from collections.abc import (
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from datetime import (
    date,
    timedelta,
)
from pathlib import Path
from typing import Any

import lane_score
from llm_wiki import runtime_roots
from llm_wiki.retrieval_graph import (
    name_dropped,
    page_name,
    query_terms,
)
from llm_wiki.retrieval_query import (
    RetrievalCandidate,
    promote_exact_filename,
)
from llm_wiki.retrieval_rerank import breadcrumb_candidate

# Files that hold independent episodes rather than one argument: a daily log
# holds every session of its day, a raw source every excerpt of its import.
_EPISODIC_ROOTS = ("knowledge/daily/", "knowledge/raw/")


def _is_episodic(relative_path: str) -> bool:
    return str(relative_path).replace("\\", "/").startswith(_EPISODIC_ROOTS)


def evidence_ordered(
    candidates: Sequence[RetrievalCandidate],
    display_meta: Mapping[str, Mapping[str, Any]],
) -> tuple[RetrievalCandidate, ...]:
    """Turns of episodes ordered by one score over the lanes; pages keep the trust order.

    The score reads what each lane said, what it did not say, whose turn it is and the
    cross-encoder where it ran (`lane_score`). It is applied to episodes only: it was fitted
    on conversation turns, while a compiled page's place is decided by the trust table.
    Measured 2026-09-16 on 189 LongMemEval questions, all evidence inside the reader's
    twelve: 0.698 before, 0.815 after. Research:
    `docs/research/2026-09-16-one-score-over-the-lanes.md`.
    """
    places = _episodic_places(candidates)
    if len(places) < 2:
        return tuple(candidates)
    ordered = sorted(
        (candidates[index] for index in places),
        key=lambda item: -lane_score.candidate_score(item, display_meta),
    )
    return tuple(_merged_places(candidates, places, ordered))


def visible_order(
    candidates: Sequence[RetrievalCandidate],
    display_meta: Mapping[str, Mapping[str, Any]],
    exact_query: str,
    query: str,
    limit: int | None,
) -> tuple[RetrievalCandidate, ...]:
    """The order a caller sees: distinct pages, episodes by lane score, the question covered.

    Coverage comes after the lane score and not before it, because the lane
    sort replaces whatever order it was given for every episodic position: on a
    corpus that is all episodes — every LongMemEval run — a rule applied
    earlier leaves no trace at all.

    The promotion is the last step because the steps before it know nothing of
    it: page diversity put a named daily file behind every compiled page, and
    the lane score then re-sorted it among the episodes, so a file asked for by
    its date was no longer the answer and the mode no longer said `EXACT`.
    Research: `docs/research/2026-09-17-a-named-file-stays-first.md`.
    """
    ordered = evidence_ordered(page_diverse(candidates), display_meta)
    covered = cover_then_fill(ordered, display_meta, query, limit)
    return promote_exact_filename(covered, exact_query)


# --- The window covers the question first ------------------------------------
#
# Before the window is filled by the lane score it holds one chunk from every
# source that covers a distinct part of the question. The parts are read from
# the question text and the store only: its terms, the days and stretches of
# days `temporal_anchor` wrote into it, the fact keys of each turn, and — when
# the question counts or sums — one part per source that mentions the counted
# kind, because a count needs every instance and each lives in its own session.
# Selection is greedy maximum coverage over sources, which reaches at least
# 1 - 1/e of the best choice (Nemhauser, Wolsey, Fisher 1978) and stops at zero
# gain; then a compiled page pulls in the episodes and pages it names; then
# the lane order fills the rest. This replaced the per-source quota of
# 2026-09-19, which paid all-turns@12 on every question and gained on few.
# Research: `docs/research/2026-09-22-the-window-covers-the-question-first.md`.

SourceKey = tuple[str, tuple[str, ...]]
Aspect = tuple[str, ...]

_ISO_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def source_key(candidate: RetrievalCandidate) -> SourceKey:
    """The entry a chunk belongs to: its file and the heading it sits under.

    The file alone is not the source. A daily file holds every session of its
    day, so by path two sessions of one day are one source and a rule keyed on
    the path would starve the second. This is the key the packer's own coverage
    rule already uses (`query_memory.entry_key`).
    """
    return (candidate.relative_path, candidate.heading_path)


@dataclass(frozen=True)
class _Aspects:
    """The parts of a question, read from its text and nothing else."""

    terms: frozenset[str]
    day_runs: tuple[tuple[str, str], ...]
    kind: str


def _question_aspects(query: str) -> _Aspects:
    """Terms minus the lexical leg's stopwords, stretches of days, the counted kind."""
    from aggregation_pass import asks_to_aggregate, counted_kind
    from generation_search import (
        QUERY_STOPWORDS,
    )

    tokens = query_terms(query)
    days = _valid_days(tokens)
    terms = frozenset(term for term in tokens if term not in QUERY_STOPWORDS and term not in days)
    kind = ""
    if asks_to_aggregate(query):
        kind = counted_kind(query)
    return _Aspects(terms, _day_runs(days), kind)


def _valid_days(tokens: frozenset[str]) -> frozenset[str]:
    return frozenset(token for token in tokens if _ISO_DAY_RE.fullmatch(token) and _is_a_day(token))


def _is_a_day(token: str) -> bool:
    try:
        date.fromisoformat(token)
    except ValueError:
        return False
    return True


def _day_runs(days: frozenset[str]) -> tuple[tuple[str, str], ...]:
    """Consecutive days are one stretch: a range the question named is one part of it."""
    runs: list[tuple[str, str]] = []
    for day in sorted(days):
        if runs and _next_day(runs[-1][1]) == day:
            runs[-1] = (runs[-1][0], day)
            continue
        runs.append((day, day))
    return tuple(runs)


def _next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def _span_of(candidate: RetrievalCandidate, display_meta: Mapping[str, Mapping[str, Any]]) -> str:
    info = display_meta.get(candidate.candidate_id) or {}
    return str(info.get("span_sha256") or "")


def _fact_keys_of(
    candidates: Sequence[RetrievalCandidate], display_meta: Mapping[str, Mapping[str, Any]]
) -> dict[str, str]:
    """The pool's fact keys by turn span, from the store; none when it is absent or unreadable."""
    spans = {_span_of(candidate, display_meta) for candidate in candidates}
    spans.discard("")
    if not spans:
        return {}
    from fact_keys import keys_for_spans, store_path

    try:
        return keys_for_spans(store_path(runtime_roots.state_root()), spans)
    except (OSError, sqlite3.Error) as exc:
        name_dropped("fact_keys", exc)
        return {}


def _coverable_text(
    candidate: RetrievalCandidate,
    display_meta: Mapping[str, Mapping[str, Any]],
    keys: Mapping[str, str],
) -> str:
    """What a chunk can cover with: its path (the day), its heading, its text, its keys."""
    return " ".join(
        (
            candidate.relative_path,
            " ".join(candidate.heading_path),
            lane_score.candidate_text(candidate, display_meta),
            keys.get(_span_of(candidate, display_meta), ""),
        )
    )


def _kind_tokens(tokens: set[str], kind: str) -> frozenset[str]:
    """The pool's tokens that name the counted kind, by the aggregation pass's own stem."""
    if not kind:
        return frozenset()
    from aggregation_pass import blocking_key

    return frozenset(token for token in tokens if blocking_key(token, kind) == kind)


def _names_a_day_in(days: frozenset[str], first: str, last: str) -> bool:
    return any(first <= day <= last for day in days)


def _covered_aspects(
    aspects: _Aspects, source: SourceKey, tokens: frozenset[str], kind_tokens: frozenset[str]
) -> frozenset[Aspect]:
    """The parts of the question one chunk covers."""
    covered: set[Aspect] = {("term", term) for term in aspects.terms & tokens}
    days = _valid_days(tokens)
    covered.update(
        ("days", first, last) for first, last in aspects.day_runs if _names_a_day_in(days, first, last)
    )
    if tokens & kind_tokens:
        covered.add(("instance", source[0], *source[1]))
    return frozenset(covered)


def _coverage_sets(
    candidates: Sequence[RetrievalCandidate],
    display_meta: Mapping[str, Mapping[str, Any]],
    aspects: _Aspects,
) -> dict[str, frozenset[Aspect]]:
    keys = _fact_keys_of(candidates, display_meta)
    tokens = {
        candidate.candidate_id: query_terms(_coverable_text(candidate, display_meta, keys))
        for candidate in candidates
    }
    kind_tokens = _kind_tokens(set().union(*tokens.values()), aspects.kind)
    return {
        candidate.candidate_id: _covered_aspects(
            aspects, source_key(candidate), tokens[candidate.candidate_id], kind_tokens
        )
        for candidate in candidates
    }


def _grouped_by_source(
    candidates: Sequence[RetrievalCandidate],
) -> dict[SourceKey, list[RetrievalCandidate]]:
    by_source: dict[SourceKey, list[RetrievalCandidate]] = {}
    for candidate in candidates:
        by_source.setdefault(source_key(candidate), []).append(candidate)
    return by_source


class _Admission:
    """What one selection has admitted so far, and what is still uncovered."""

    def __init__(self, coverage: Mapping[str, frozenset[Aspect]], room: int) -> None:
        self.coverage = coverage
        self.room = room
        self.uncovered: set[Aspect] = set().union(*coverage.values())
        self.taken: set[SourceKey] = set()
        self.admitted: list[RetrievalCandidate] = []

    def admit(self, chunk: RetrievalCandidate) -> None:
        self.admitted.append(chunk)
        self.taken.add(source_key(chunk))
        self.uncovered -= self.coverage[chunk.candidate_id]
        self.room -= 1

    def gain(self, chunk: RetrievalCandidate) -> int:
        return len(self.coverage[chunk.candidate_id] & self.uncovered)


def _best_chunk(
    chunks: Sequence[RetrievalCandidate], state: _Admission
) -> tuple[int, RetrievalCandidate]:
    """One source's chunk covering most of what is still uncovered; ties go to the lane-best."""
    best = chunks[0]
    gain = state.gain(best)
    for chunk in chunks[1:]:
        more = state.gain(chunk)
        if more > gain:
            best, gain = chunk, more
    return gain, best


def _best_source(
    by_source: Mapping[SourceKey, Sequence[RetrievalCandidate]], state: _Admission
) -> RetrievalCandidate | None:
    """The chunk of the source with the largest gain, or None when no gain is left."""
    gain, best = 0, None
    for source, chunks in by_source.items():
        if source in state.taken:
            continue
        more, chunk = _best_chunk(chunks, state)
        if more > gain:
            gain, best = more, chunk
    return best


def _greedy_cover(
    by_source: Mapping[SourceKey, Sequence[RetrievalCandidate]], state: _Admission
) -> None:
    """Greedy maximum coverage: the source with the most uncovered parts, until none is left."""
    while state.room > 0:
        chunk = _best_source(by_source, state)
        if chunk is None:
            return
        state.admit(chunk)


def _referenced_blocks(text: str) -> frozenset[tuple[str, str]]:
    """The (day, block) of every canonical evidence reference a page carries.

    The `## Evidence` lines and the `## Claims` ledger of a compiled page both
    name their source block this way (`compile_pipeline_claims.evidence_lines`,
    `derived_claim`). The resolver's own pattern finds them; the resolver's
    strict reader is not used because a chunk may cut a reference in half.
    """
    from llm_wiki.evidence_reference import REF_RE

    return frozenset((match["daily"], match["block"]) for match in REF_RE.finditer(text))


def _names_a_block(path: str, heading: str, blocks: frozenset[tuple[str, str]]) -> bool:
    day = Path(path).stem
    return any(day == named_day and f"[{block}]" in heading for named_day, block in blocks)


def _points_at(source: SourceKey, blocks: frozenset[tuple[str, str]], pages: frozenset[str]) -> bool:
    path, heading = source
    if _is_episodic(path):
        return _names_a_block(path, " ".join(heading), blocks)
    return page_name(path) in pages


def _pointed_sources(
    chunk: RetrievalCandidate,
    display_meta: Mapping[str, Mapping[str, Any]],
    by_source: Mapping[SourceKey, Sequence[RetrievalCandidate]],
) -> list[SourceKey]:
    """The pool's sources a compiled page names: its evidence blocks and its wikilinks."""
    if _is_episodic(chunk.relative_path):
        return []
    from co_activation import links_in

    text = lane_score.candidate_text(chunk, display_meta)
    blocks = _referenced_blocks(text)
    pages = frozenset(links_in(text))
    return [source for source in by_source if _points_at(source, blocks, pages)]


def _admit_pointed(
    source: SourceKey,
    by_source: Mapping[SourceKey, Sequence[RetrievalCandidate]],
    state: _Admission,
) -> None:
    if state.room <= 0 or source in state.taken:
        return
    _gain, chunk = _best_chunk(by_source[source], state)
    state.admit(chunk)


def _complete_from_pages(
    by_source: Mapping[SourceKey, Sequence[RetrievalCandidate]],
    state: _Admission,
    display_meta: Mapping[str, Mapping[str, Any]],
) -> None:
    """Index-driven completion: an admitted page pulls in the episodes and pages it names.

    One hop, from the pages the coverage step admitted; what those pull in does
    not pull further. Teyler & Rudy 2007: a partial cue that activates the index
    retrieves the episode; the page is the index, its references the cue.
    """
    for chunk in list(state.admitted):
        for source in _pointed_sources(chunk, display_meta, by_source):
            _admit_pointed(source, by_source, state)


def _may_displace(
    chunk: RetrievalCandidate, admitted: frozenset[str], inside_count: Mapping[SourceKey, int]
) -> bool:
    if chunk.candidate_id in admitted:
        return False
    return inside_count.get(source_key(chunk), 0) >= 2


def _displaceable(
    inside: Sequence[RetrievalCandidate], admitted: frozenset[str], wanted: int
) -> list[RetrievalCandidate]:
    """The last chunks of the window a pulled chunk may take the place of.

    Never an admitted chunk, and never a source's only chunk in the window: a
    chunk is displaced only while its source keeps another chunk inside.
    """
    inside_count: dict[SourceKey, int] = {}
    for chunk in inside:
        inside_count[source_key(chunk)] = inside_count.get(source_key(chunk), 0) + 1
    chosen: list[RetrievalCandidate] = []
    for chunk in reversed(inside):
        if len(chosen) >= wanted:
            break
        if _may_displace(chunk, admitted, inside_count):
            chosen.append(chunk)
            inside_count[source_key(chunk)] -= 1
    return list(reversed(chosen))


def _pulled_into_window(
    candidates: Sequence[RetrievalCandidate], admitted: frozenset[str], limit: int
) -> tuple[RetrievalCandidate, ...]:
    """Admitted chunks outside the window take the places of its last displaceable chunks.

    Admitted chunks already inside keep their place, the pulled ones come in
    behind the kept ones in lane order, the displaced ones follow, and nothing
    is dropped: `_capped` still cuts the window last. When more is admitted
    than can be displaced, the lane-last of the pulled stay outside.
    """
    inside, outside = list(candidates[:limit]), list(candidates[limit:])
    displaced = _displaceable(inside, admitted, len(_only(outside, admitted)))
    pulled = _only(outside, admitted)[: len(displaced)]
    if not pulled:
        return tuple(candidates)
    moved = _ids_of([*pulled, *displaced])
    return tuple([*_without(inside, moved), *pulled, *displaced, *_without(outside, moved)])


def _ids_of(chunks: Sequence[RetrievalCandidate]) -> frozenset[str]:
    return frozenset(chunk.candidate_id for chunk in chunks)


def _only(chunks: Sequence[RetrievalCandidate], ids: frozenset[str]) -> list[RetrievalCandidate]:
    return [chunk for chunk in chunks if chunk.candidate_id in ids]


def _without(chunks: Sequence[RetrievalCandidate], ids: frozenset[str]) -> list[RetrievalCandidate]:
    return [chunk for chunk in chunks if chunk.candidate_id not in ids]


def _fits_the_window(candidates: Sequence[RetrievalCandidate], limit: int | None) -> bool:
    """No window, or a pool no larger than it: there is nothing to make room for."""
    if limit is None or int(limit) <= 0:
        return True
    return len(candidates) <= int(limit)


def cover_then_fill(
    candidates: Sequence[RetrievalCandidate],
    display_meta: Mapping[str, Mapping[str, Any]],
    query: str,
    limit: int | None,
) -> tuple[RetrievalCandidate, ...]:
    """One chunk from every source that covers a distinct part of the question, then the fill.

    Four properties, each pinned by a test: a pool holding one source comes out
    unchanged; a pool that fits the window comes out unchanged; a source's only
    chunk inside the window is never displaced; a source's chunks keep their
    order, except the one chunk admitted for coverage, which is the lane-best
    whenever the lane-best covers as much.
    """
    if _fits_the_window(candidates, limit):
        return tuple(candidates)
    by_source = _grouped_by_source(candidates)
    if len(by_source) < 2:
        return tuple(candidates)
    state = _Admission(_coverage_sets(candidates, display_meta, _question_aspects(query)), int(limit))
    _greedy_cover(by_source, state)
    _complete_from_pages(by_source, state, display_meta)
    return _pulled_into_window(candidates, _ids_of(state.admitted), int(limit))


def _episodic_places(candidates: Sequence[RetrievalCandidate]) -> list[int]:
    return [index for index, item in enumerate(candidates) if _is_episodic(item.relative_path)]


def _merged_places(
    candidates: Sequence[RetrievalCandidate],
    places: Sequence[int],
    ordered: Sequence[RetrievalCandidate],
) -> list[RetrievalCandidate]:
    merged = list(candidates)
    for place, candidate in zip(places, ordered):
        merged[place] = candidate
    return merged


def _place_by_page(
    candidate: RetrievalCandidate,
    seen: set[tuple],
    first: list[RetrievalCandidate],
    extras: list[RetrievalCandidate],
) -> None:
    """A page's first chunk takes a slot; a chunk of an episode keeps its rank.

    A compiled page's headings are sections of one argument, so its later chunks
    follow the distinct pages: measured 2026-09-13 on the installed vault, ten
    visible rows held six pages, one workflow note taking ranks 3, 4 and 5
    (`docs/research/2026-09-13-one-argument-one-slot.md`).

    An episode is not one argument. A conversation is cut into turns and its
    turns hold different facts; giving a session one slot pushed every later
    turn behind the first chunk of every other session. Measured 2026-09-15 on
    189 LongMemEval questions, all evidence turns in the first twelve rows: 0.392
    at one slot per session, 0.566 at two, 0.608 at three, 0.698 in relevance
    order, which no concave per-session reward beat. A reader that needs every
    fact is served by relevance order, not by diversity.
    Research: `docs/research/2026-09-15-what-a-slot-should-reward.md`.
    """
    if _is_episodic(candidate.relative_path):
        first.append(candidate)
        return
    entry = (candidate.relative_path,)
    if entry in seen:
        extras.append(candidate)
        return
    seen.add(entry)
    first.append(candidate)


def _supporting(candidate: RetrievalCandidate) -> bool:
    """Kinds the trust table puts below neutral: raw evidence and gap stubs.

    They support an answer rather than leading it, which is the vault's own
    retrieval rule — answer from the compiled pages, read raw material only when
    the pages are missing, stale or contradictory. Measured: importing 236 past
    sessions filled every place with transcripts of the discussions the decision
    pages were compiled from, and the stand fell from hit@5 0.7 to 0.0.
    """
    return candidate.type_weight < 1.0


def page_diverse(
    candidates: Sequence[RetrievalCandidate],
) -> tuple[RetrievalCandidate, ...]:
    """One chunk per page first, then every chunk that repeats a page.

    This is the first step of `visible_order`, and it is where a page is stopped
    from taking several visible slots. Chunks of episodes are not repeats and keep
    their rank (`_place_by_page`). Nothing is dropped — the repeats follow the
    first pass — so a caller that wanted every chunk of one page still receives
    them, in order. The remedies that compare candidates to each other (maximal
    marginal relevance, semantic deduplication) are not needed here: the
    duplication is structural.

    A slot is claimed in `_diversity_groups` order, which keeps the reranker's
    judgement ahead of the pool it never read.
    """
    first: list[RetrievalCandidate] = []
    extras: list[RetrievalCandidate] = []
    seen: set[tuple] = set()
    for group in _diversity_groups(candidates):
        _place_group(group, seen, first, extras)
    return tuple(first + extras)


def _place_group(
    group: Sequence[RetrievalCandidate],
    seen: set[tuple],
    first: list[RetrievalCandidate],
    extras: list[RetrievalCandidate],
) -> None:
    for candidate in group:
        _place_by_page(candidate, seen, first, extras)


def _diversity_groups(
    candidates: Sequence[RetrievalCandidate],
) -> tuple[list[RetrievalCandidate], ...]:
    """Four passes over the pool, strongest claim on a visible slot first.

    What the reranker actually scored leads what it never saw: a slot is filled
    from the bounded pool it read before the tail below that pool is used to
    replace a repeat. Within each of those, the vault's own rule holds —
    compiled pages before raw evidence. With no reranking, or no tail, every
    candidate lands in one group and the order is the one this always had.
    """
    groups: list[list[RetrievalCandidate]] = []
    for tier in _scored_then_unseen(candidates):
        groups.extend(_reranked_source_kinds(tier))
    return tuple(groups)


def _reranked_source_kinds(candidates):
    """A scored event competes by relevance; unscored evidence retains its tier."""
    if any(breadcrumb_candidate(item) and item.rerank_score is not None for item in candidates):
        return (list(candidates),)
    return _by_kind(candidates)


def _scored_then_unseen(
    candidates: Sequence[RetrievalCandidate],
) -> tuple[list[RetrievalCandidate], list[RetrievalCandidate]]:
    """What the reranker scored, then what it never read."""
    return (
        [item for item in candidates if item.rerank_score is not None],
        [item for item in candidates if item.rerank_score is None],
    )


def _by_kind(
    candidates: Sequence[RetrievalCandidate],
) -> tuple[list[RetrievalCandidate], list[RetrievalCandidate]]:
    """Compiled pages lead; raw evidence and gap stubs support."""
    return (
        [item for item in candidates if not _supporting(item)],
        [item for item in candidates if _supporting(item)],
    )


def require_bounded_int(value: object, low: int, high: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be between {low} and {high}")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
